"""시딩 + AI 배치 매핑 Mixin."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from backend.domain.samba.category.rules import (
    MARKET_CATEGORIES,
    _detect_gender,
    _expand_synonyms,
    _filter_overseas,
    _filter_to_leaves,
    _gender_balanced_cap,
    _rule_match,
    _similarity_match_smartstore,
)

logger = logging.getLogger(__name__)


def _build_fewshot_block(
    batch: List[Dict[str, Any]],
    target_markets: List[str],
) -> str:
    """EXPORTED_RULES에서 같은 소싱사이트+대분류 기준으로 학습 예시를 추출.

    반환값은 프롬프트에 그대로 삽입할 문자열 (비어 있으면 "").
    """
    try:
        from backend.domain.samba.category.rules_exported import EXPORTED_RULES
    except ImportError:
        return ""

    if not EXPORTED_RULES:
        return ""

    examples: list[str] = []
    seen: set[str] = set()

    for item in batch:
        site = item.get("site", "")
        leaf_path = item.get("leaf_path", "")
        if not site or not leaf_path:
            continue
        # 소싱 카테고리 대분류(cat1) 기준으로 유사 예시 추출
        cat1 = leaf_path.split(" > ")[0].strip()

        for market in target_markets:
            exported = EXPORTED_RULES.get((site, market), {})
            count = 0
            for src, tgt in exported.items():
                if not src.startswith(cat1):
                    continue
                # 자기 자신은 제외
                if src == leaf_path:
                    continue
                key = f"{site}|{src}|{market}|{tgt}"
                if key in seen or count >= 3:
                    continue
                seen.add(key)
                examples.append(f"  [{site}] {src} → {market}: {tgt}")
                count += 1
            if len(examples) >= 8:
                break
        if len(examples) >= 8:
            break

    if not examples:
        return ""

    logger.info("[AI매핑] fewshots %d건 주입", len(examples))
    block = "\n[학습 예시 — 아래 패턴과 일관되게 매핑하세요]\n"
    block += "\n".join(examples[:8])
    block += "\n"
    return block


class CategorySeedMixin:
    """시딩 + AI 배치 매핑."""

    async def _get_db_fewshot_examples(
        self,
        source_sites: list[str],
        target_markets: list[str],
        exclude_cat: str | None = None,
        limit: int = 8,
    ) -> str:
        """DB 매핑 데이터에서 few-shot 예시 추출.

        타겟 마켓에 기존 매핑이 부족하면(예: 11번가 첫 매핑) 크로스마켓 패턴을 폴백으로 사용.
        — 같은 소싱처의 다른 마켓 매핑(스스/롯데ON 등)을 동일 소싱카테고리 기준으로 묶어 보여주어
          AI가 "스스가 X면 11번가도 비슷한 의미"로 추론하게 한다.
        """
        from sqlmodel import select
        from backend.domain.samba.category.model import SambaCategoryMapping

        # ── 1차: 타겟 마켓 직접 매핑 (기존 동작) ──
        examples: list[str] = []
        seen: set[str] = set()

        for site in source_sites:
            stmt = (
                select(SambaCategoryMapping)
                .where(SambaCategoryMapping.source_site == site)
                .limit(200)
            )
            result = await self.mapping_repo.session.execute(stmt)
            rows = result.scalars().all()
            for row in rows:
                if not row.target_mappings or not isinstance(row.target_mappings, dict):
                    continue
                src_cat = (row.source_category or "").strip()
                if not src_cat or src_cat == exclude_cat:
                    continue
                for market in target_markets:
                    tgt = row.target_mappings.get(market, "")
                    if not tgt or " > " not in tgt:
                        continue
                    key = f"{site}|{src_cat}|{market}|{tgt}"
                    if key in seen:
                        continue
                    seen.add(key)
                    examples.append(f"  [{site}] {src_cat} → {market}: {tgt}")
                    if len(examples) >= limit:
                        break
                if len(examples) >= limit:
                    break
            if len(examples) >= limit:
                break

        # ── 2차: 크로스마켓 폴백 ──
        # 타겟 매핑이 적으면(첫 매핑 케이스), 같은 소싱카테고리에 대한 다른 마켓 매핑을
        # 묶어서 보여줘 패턴 학습 유도. 예: "[GSShop] 신발>운동화 → smartstore: A | lotteon: B"
        cross_examples: list[str] = []
        if len(examples) < limit:
            cross_seen: set[str] = set()
            cross_limit = limit - len(examples)
            for site in source_sites:
                stmt = (
                    select(SambaCategoryMapping)
                    .where(SambaCategoryMapping.source_site == site)
                    .limit(300)
                )
                result = await self.mapping_repo.session.execute(stmt)
                rows = result.scalars().all()
                for row in rows:
                    if not row.target_mappings or not isinstance(
                        row.target_mappings, dict
                    ):
                        continue
                    src_cat = (row.source_category or "").strip()
                    if not src_cat or src_cat == exclude_cat:
                        continue
                    # 타겟 마켓에 매핑이 이미 있으면 1차에서 처리됨 — 패턴 학습용으론 미매핑 케이스 활용
                    has_target = any(
                        row.target_mappings.get(m)
                        and " > " in row.target_mappings.get(m)
                        for m in target_markets
                    )
                    if has_target:
                        continue
                    # 다른 마켓 매핑 2개 이상 있어야 패턴 학습 가치 있음
                    other_pairs = []
                    for mk, val in row.target_mappings.items():
                        if (
                            mk not in target_markets
                            and val
                            and isinstance(val, str)
                            and " > " in val
                        ):
                            other_pairs.append(f"{mk}:{val}")
                    if len(other_pairs) < 2:
                        continue
                    key = f"{site}|{src_cat}"
                    if key in cross_seen:
                        continue
                    cross_seen.add(key)
                    cross_examples.append(
                        f"  [{site}] {src_cat} → " + " | ".join(other_pairs[:4])
                    )
                    if len(cross_examples) >= cross_limit:
                        break
                if len(cross_examples) >= cross_limit:
                    break

        if not examples and not cross_examples:
            return ""

        logger.info(
            "[AI매핑] DB few-shot 직접=%d, 크로스마켓=%d 주입",
            len(examples),
            len(cross_examples),
        )
        out = ""
        if examples:
            out += (
                "\n[기존 매핑 참고 예시 — 동일 소싱처의 확정된 매핑]\n"
                + "\n".join(examples)
                + "\n"
            )
        if cross_examples:
            out += (
                "\n[크로스마켓 패턴 — 다른 마켓 매핑을 참고해 동일 의미·동일 깊이로 매핑]\n"
                + "\n".join(cross_examples)
                + "\n"
            )
        return out

    # ==================== Market Category Seed ====================

    async def seed_market_categories(self) -> Dict[str, int]:
        """MARKET_CATEGORIES 하드코딩 데이터를 DB SambaCategoryTree에 저장.

        기존 DB 데이터가 있으면 병합 (중복 제거).
        Returns: { market: category_count } 딕셔너리
        """
        result: Dict[str, int] = {}
        for market, cats in MARKET_CATEGORIES.items():
            existing = await self.tree_repo.get_by_site(market)
            if existing:
                db_cats = existing.cat1 or []
                merged = list(dict.fromkeys(db_cats + cats))
                existing.cat1 = merged
                existing.updated_at = datetime.now(UTC)
                self.tree_repo.session.add(existing)
                result[market] = len(merged)
            else:
                await self.tree_repo.create_async(
                    site_name=market,
                    cat1=cats,
                )
                result[market] = len(cats)
        await self.tree_repo.session.commit()
        return result

    async def seed_smartstore_from_api(self, session: "AsyncSession") -> Dict[str, Any]:
        """스마트스토어 실제 카테고리를 API에서 가져와 DB에 저장.

        GET /v1/categories?last=false → wholeCategoryName으로 카테고리 경로 구성.
        """
        from backend.domain.samba.proxy.smartstore import SmartStoreClient
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository
        from backend.domain.samba.account.model import SambaMarketAccount
        from sqlmodel import select

        # 스마트스토어 계정 찾기
        stmt = select(SambaMarketAccount).where(
            SambaMarketAccount.market_type == "smartstore",
            SambaMarketAccount.is_active == True,
        )
        result = await session.execute(stmt)
        account = result.scalars().first()
        if not account:
            return {"error": "활성 스마트스토어 계정이 없습니다"}

        extras = account.additional_fields or {}
        client_id = extras.get("clientId", "") or account.api_key or ""
        client_secret = extras.get("clientSecret", "") or account.api_secret or ""

        if not client_id or not client_secret:
            # Settings 테이블 폴백
            settings_repo = SambaSettingsRepository(session)
            row = await settings_repo.find_by_async(key="smartstore")
            if row and isinstance(row.value, dict):
                client_id = client_id or row.value.get("clientId", "")
                client_secret = client_secret or row.value.get("clientSecret", "")

        if not client_id or not client_secret:
            return {"error": "스마트스토어 API 인증 정보가 없습니다"}

        client = SmartStoreClient(client_id=client_id, client_secret=client_secret)

        # API에서 전체 카테고리 조회
        try:
            api_cats = await client.get_categories(last_only=False)
        except Exception as e:
            return {"error": f"카테고리 API 호출 실패: {e}"}

        if not isinstance(api_cats, list):
            return {"error": "카테고리 API 응답 형식 오류"}

        # wholeCategoryName → 카테고리 경로, id → 코드
        categories: list[str] = []
        code_map: Dict[str, str] = {}
        for cat in api_cats:
            whole_name = cat.get("wholeCategoryName", "")
            cat_id = cat.get("id", "")
            if whole_name:
                # API 형식: "패션잡화>남성신발>스니커즈" → "패션잡화 > 남성신발 > 스니커즈"
                path = " > ".join(p.strip() for p in whole_name.split(">"))
                categories.append(path)
                if cat_id:
                    code_map[path] = str(cat_id)

        if not categories:
            return {"error": "가져온 카테고리가 없습니다"}

        # DB 저장 (기존 데이터 교체)
        existing = await self.tree_repo.get_by_site("smartstore")
        if existing:
            existing.cat1 = categories
            existing.cat2 = code_map
            existing.updated_at = datetime.now(UTC)
            self.tree_repo.session.add(existing)
        else:
            await self.tree_repo.create_async(
                site_name="smartstore",
                cat1=categories,
                cat2=code_map,
            )
        await session.commit()

        logger.info(
            f"[카테고리] 스마트스토어 API에서 {len(categories)}개 카테고리 동기화 완료"
        )
        return {"ok": True, "count": len(categories), "has_codes": bool(code_map)}

    async def seed_market_via_ai(
        self, market_type: str, api_key: str
    ) -> Dict[str, Any]:
        """AI로 마켓의 전체 카테고리 목록을 생성하여 DB에 저장.

        계정/API 없는 마켓도 Claude가 실제 카테고리 체계를 알고 있으므로
        서비스 운영자가 미리 DB를 채워놓을 수 있다.
        """
        import anthropic

        market_label = {
            "smartstore": "네이버 스마트스토어",
            "coupang": "쿠팡",
            "gmarket": "G마켓",
            "auction": "옥션",
            "11st": "11번가",
            "ssg": "SSG(신세계몰)",
            "lotteon": "롯데ON",
            "lottehome": "롯데홈쇼핑",
            "gsshop": "GS샵",
            "homeand": "홈앤쇼핑",
            "hmall": "HMALL(현대홈쇼핑)",
            "kream": "KREAM",
            "ebay": "eBay Korea",
            "lazada": "Lazada",
            "qoo10": "Qoo10",
            "shopee": "Shopee",
        }.get(market_type, market_type)

        prompt = f"""{market_label}의 실제 상품 카테고리 전체 목록을 작성해주세요.

규칙:
1. 실제 {market_label} 셀러센터에서 상품 등록 시 선택하는 카테고리 체계를 따르세요.
2. "대분류 > 중분류 > 소분류 > 세분류" 형태의 전체 경로로 작성하세요.
3. 최하위(리프) 카테고리까지 모두 포함하세요.
4. 주요 카테고리를 빠짐없이 작성하세요 (패션, 뷰티, 식품, 가전, 생활, 스포츠 등).
5. 특히 패션(의류/신발/잡화)과 뷰티(스킨케어/메이크업/헤어/바디) 카테고리는 세분류까지 상세하게 작성하세요.
6. 최소 200개 이상의 리프 카테고리를 포함해주세요.
7. "해외직구", "해외", "해외호텔", "해외여행" 등 해외 관련 카테고리는 절대 포함하지 마세요.
8. JSON 배열만 응답하세요.

예시: ["패션의류 > 여성의류 > 원피스", "뷰티 > 메이크업 > 블러셔", ...]"""

        client = anthropic.AsyncAnthropic(api_key=api_key)
        # 429 rate limit 대비 재시도
        for attempt in range(3):
            try:
                response = await client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=8192,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError:
                if attempt < 2:
                    logger.warning(
                        "Claude API 429 rate limit — %d초 후 재시도 (%d/3)",
                        60 * (attempt + 1),
                        attempt + 1,
                    )
                    await asyncio.sleep(60 * (attempt + 1))
                else:
                    raise

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

        categories = json.loads(text)
        if not isinstance(categories, list) or not categories:
            raise ValueError("AI 응답에서 카테고리 목록을 파싱할 수 없습니다")

        categories = _filter_overseas([str(c) for c in categories if c])

        # DB에 병합 저장
        existing = await self.tree_repo.get_by_site(market_type)
        if existing:
            db_cats = existing.cat1 or []
            merged = list(dict.fromkeys(db_cats + categories))
            existing.cat1 = merged
            existing.updated_at = datetime.now(UTC)
            self.tree_repo.session.add(existing)
            count = len(merged)
        else:
            await self.tree_repo.create_async(site_name=market_type, cat1=categories)
            count = len(categories)
        await self.tree_repo.session.commit()

        logger.info("[AI 시드] %s: %d개 카테고리 생성/병합", market_type, count)
        return {"market": market_type, "count": count, "new": len(categories)}

    async def seed_all_markets_via_ai(self, api_key: str) -> Dict[str, Any]:
        """모든 마켓의 카테고리를 AI로 일괄 생성."""
        markets = list(MARKET_CATEGORIES.keys())
        results: Dict[str, Any] = {}
        for market in markets:
            try:
                result = await self.seed_market_via_ai(market, api_key)
                results[market] = {"ok": True, **result}
            except Exception as e:
                results[market] = {"ok": False, "error": str(e)}
                logger.warning("[AI 시드] %s 실패: %s", market, e)
        return results

    # ==================== Batch AI Category Suggestion ====================

    async def _batch_ai_suggest(
        self,
        items: List[Dict[str, Any]],
        target_markets: List[str],
        api_key: str,
    ) -> List[Any]:
        """여러 카테고리를 배치로 묶어 1회 AI 호출로 처리.

        카테고리 목록을 프롬프트에 넣지 않음 — Claude가 각 마켓의 카테고리 체계를 알고 있으므로
        소싱 카테고리와 상품명만 전달하면 충분. 토큰 대폭 절감.
        10개씩 배치, 배치 간 3초 딜레이.
        """
        import anthropic
        from backend.core.config import settings

        key = api_key or settings.anthropic_api_key
        if not key:
            return ["API 키 없음"] * len(items)

        # 마켓 한글명 매핑 (프롬프트에서 마켓 식별용)
        market_labels: Dict[str, str] = {
            "smartstore": "네이버 스마트스토어",
            "coupang": "쿠팡",
            "gmarket": "G마켓",
            "auction": "옥션",
            "11st": "11번가",
            "ssg": "SSG(신세계몰)",
            "lotteon": "롯데ON",
            "lottehome": "롯데홈쇼핑",
            "gsshop": "GS샵",
        }
        market_names = ", ".join(market_labels.get(m, m) for m in target_markets)

        # 키즈/주니어 키워드 — 소싱·후보 양쪽에서 동일하게 식별
        _kids_keywords = (
            "주니어",
            "아동",
            "유아",
            "베이비",
            "키즈",
            "kids",
            "junior",
            "baby",
        )

        def _is_kids(text: str) -> bool:
            t = (text or "").lower()
            return any(kw in t for kw in _kids_keywords)

        # DB에서 마켓별 실제 카테고리 목록 조회 (AI가 이 중에서만 선택, 리프만 허용)
        # 키즈 카테고리는 전역 차단하지 않음 — 소싱이 키즈면 키즈 매핑이 정답이기 때문.
        # 후보 풀 구성 시 소싱-키즈 여부에 맞춰 동적으로 필터링한다.
        market_cat_lists: Dict[str, List[str]] = {}
        for m in target_markets:
            try:
                cats = _filter_to_leaves(await self._get_market_categories(m))
                if cats:
                    # 모든 마켓 공통: 브랜드/명품/디자이너/해외직구 접두어 카테고리 제외
                    _exclude_prefixes = (
                        "해외직구",
                        "브랜드",
                        "명품",
                        "수입명품",
                        "디자이너",
                        "도서",
                        "음반",
                    )
                    cats = [
                        c
                        for c in cats
                        if not any(c.startswith(p) for p in _exclude_prefixes)
                    ]
                    market_cat_lists[m] = cats
            except Exception:
                pass

        client = anthropic.AsyncAnthropic(api_key=key)
        all_results: List[Any] = []
        # 카테고리 목록 포함 시 배치 크기 1로 축소.
        # — 배치로 묶으면 다른 item의 leaf_kw가 후보 풀에 섞여 noise 발생 (예: 신발+잡화+의류 키워드 통합 → AI 혼동).
        # — 단건 호출은 토큰 비용 비슷(2048→512), 5초 딜레이 → 1초로 단축, 정확도 보장.
        # 카테고리 목록 미포함 시(레거시) 기존 batch_size=10 유지.
        has_cat_list = bool(market_cat_lists)
        batch_size = 1 if has_cat_list else 10
        batch_delay_sec = 1 if has_cat_list else 5

        # DB 기존 매핑 few-shot — 배치 전체에 공통 적용 (한 번만 조회)
        batch_sites = list({item["site"] for item in items})
        _db_fewshot_global = await self._get_db_fewshot_examples(
            batch_sites, target_markets, limit=16
        )

        for batch_start in range(0, len(items), batch_size):
            batch = items[batch_start : batch_start + batch_size]

            # 배치 내 키즈 소싱 비율 — 모두 키즈/모두 어른/혼합 판별
            batch_kids_flags = [
                _is_kids(item.get("leaf_path", ""))
                or any(_is_kids(r) for r in (item.get("mapped_refs") or {}).values())
                for item in batch
            ]
            batch_all_kids = all(batch_kids_flags) and len(batch_kids_flags) > 0
            batch_any_kids = any(batch_kids_flags)

            cat_entries = []
            for idx, item in enumerate(batch):
                tag_str = ", ".join(item.get("tags", [])[:5])
                seo_str = ", ".join(item.get("seo", [])[:5])
                group_str = ", ".join(item.get("groups", [])[:3])
                sample_names = [n for n in (item.get("samples") or []) if n][:2]
                gender_hint = {
                    "male": "남성",
                    "female": "여성",
                    "unisex": "남녀공용",
                }.get(item.get("gender", ""), "")
                ss_hint = item.get("ss_mapped", "")
                mapped_refs = item.get("mapped_refs", {})
                is_kids = batch_kids_flags[idx]
                age_hint = "키즈/주니어" if is_kids else "성인"
                entry = f"{idx + 1}. [{item['site']}] {item['leaf_path']}"
                entry += f" | 연령: {age_hint}"
                if gender_hint:
                    entry += f" | 성별: {gender_hint}"
                if sample_names:
                    entry += f" | 상품명: {' / '.join(sample_names)}"
                # 기존 매핑된 타 마켓 참고 (ss_mapped 포함)
                if mapped_refs:
                    refs_str = ", ".join(
                        f"{mk}:{val}" for mk, val in list(mapped_refs.items())[:4]
                    )
                    entry += f" | 기존매핑참고: {refs_str}"
                elif ss_hint:
                    entry += f" | 스마트스토어매핑: {ss_hint}"
                if seo_str:
                    entry += f" | SEO: {seo_str}"
                if tag_str:
                    entry += f" | 태그: {tag_str}"
                if group_str:
                    entry += f" | 그룹: {group_str}"
                cat_entries.append(entry)

            # 마켓별 카테고리 필터 — leaf 키워드 우선 + 동의어 확장
            cat_list_section = ""
            if has_cat_list:
                import re as _re

                def _split_kw(text: str) -> list[str]:
                    """슬래시/공백/괄호 등으로 분리해 개별 키워드 추출 (2자 이상)."""
                    parts = _re.split(r"[/\s,()·\-]+", text)
                    return [p.strip() for p in parts if len(p.strip()) >= 2]

                # leaf 키워드: 각 아이템의 마지막 세그먼트를 단어 단위로 분리
                leaf_kw: set[str] = set()
                parent_kw: set[str] = set()
                for item in batch:
                    segs = [
                        s.strip() for s in item["leaf_path"].split(">") if s.strip()
                    ]
                    if segs:
                        # 마지막 세그먼트를 통째로 + 단어 분리 모두 추가
                        leaf_kw.add(segs[-1])
                        leaf_kw.update(_split_kw(segs[-1]))
                        for s in segs[:-1]:
                            if len(s) >= 2:
                                parent_kw.add(s)
                                parent_kw.update(_split_kw(s))
                    for t in (item.get("tags") or [])[:3]:
                        if t and len(t) >= 2:
                            leaf_kw.add(t)
                    for kw in (item.get("seo") or [])[:5]:
                        if kw and len(kw) >= 2:
                            leaf_kw.add(kw)
                    for g in (item.get("groups") or [])[:3]:
                        if g and len(g) >= 2:
                            leaf_kw.add(g)
                    # 상품명 키워드 추가 — "기타 하의" 같은 모호한 카테고리 보완
                    for name in (item.get("samples") or [])[:2]:
                        if not name:
                            continue
                        for part in name.replace("/", " ").replace("-", " ").split():
                            if len(part) >= 2:
                                leaf_kw.add(part)
                # priority_kw — 후보 풀 구성 시 가중치 3점으로 점수 매칭하여 후보 상위에 배치.
                # 두 신호 합산: (a) source category leaf — 상품 유형(축구복/상하복세트 등) 정확 매칭
                # (b) mapped_refs leaf — 다른 마켓 매핑과 동일 의미 영역
                # refs가 빈약/잘못된 경우(예: '축구용품')에도 source 신호로 보정 가능.
                priority_kw: set[str] = set()
                for item in batch:
                    # (a) source category leaf
                    src_segs = [
                        s.strip() for s in item["leaf_path"].split(">") if s.strip()
                    ]
                    if src_segs:
                        priority_kw.add(src_segs[-1])
                        priority_kw.update(_split_kw(src_segs[-1]))
                    # (b) mapped_refs leaf
                    for ref_path in (item.get("mapped_refs") or {}).values():
                        if not ref_path:
                            continue
                        ref_segs = [
                            s.strip() for s in str(ref_path).split(" > ") if s.strip()
                        ]
                        if not ref_segs:
                            continue
                        priority_kw.add(ref_segs[-1])
                        priority_kw.update(_split_kw(ref_segs[-1]))
                        leaf_kw.add(ref_segs[-1])
                        leaf_kw.update(_split_kw(ref_segs[-1]))
                        # parent segments(성별/연령 매칭용)는 parent_kw로
                        for seg in ref_segs[:-1]:
                            if len(seg) >= 2:
                                parent_kw.add(seg)
                                parent_kw.update(_split_kw(seg))

                # 동의어 확장 — 소싱 키워드와 마켓 카테고리 용어 차이 보완
                leaf_kw = _expand_synonyms(leaf_kw)
                parent_kw = _expand_synonyms(parent_kw)
                priority_kw = _expand_synonyms(priority_kw)

                # 배치 내 소싱 카테고리 원문 (특수 대분류 제외 판별용)
                batch_source_text = " ".join(
                    item["leaf_path"].lower() for item in batch
                )

                # 소싱에 없는 특수 대분류 제외 (2단계와 동일 로직)
                _AI_RESTRICTED_TOPS = [
                    (
                        ["유아동", "유아", "아동", "키즈"],
                        ["유아", "아동", "키즈", "주니어", "베이비"],
                    ),
                    (
                        ["자동차", "모터바이크"],
                        ["자동차", "차량", "모터바이크", "바이크", "오토바이"],
                    ),
                    (
                        ["반려동물", "강아지", "고양이"],
                        ["반려", "강아지", "고양이", "펫"],
                    ),
                    (["수입명품"], ["명품", "럭셔리", "수입명품"]),
                    (["브랜드 "], ["브랜드"]),
                    (
                        ["노트북", "데스크탑", "PC주변"],
                        ["노트북", "데스크탑", "PC", "컴퓨터"],
                    ),
                    (["모니터", "프린터"], ["모니터", "프린터"]),
                    (["저장장치"], ["저장장치", "SSD", "HDD"]),
                    (["영상가전", "계절가전"], ["가전", "TV", "에어컨"]),
                    (["음향기기"], ["스피커", "이어폰", "헤드폰", "음향"]),
                ]

                def _ai_filter_restricted(top_seg: str) -> bool:
                    top_lower = top_seg.lower()
                    for top_kws, require_kws in _AI_RESTRICTED_TOPS:
                        if any(tk in top_lower for tk in top_kws):
                            if not any(rk in batch_source_text for rk in require_kws):
                                return True
                    return False

                # 마켓별로 독립적으로 후보 풀 구성 — 한 마켓이 sparse해도 다른 마켓은 영향 받지 않음
                # 키워드 매칭 부족 시 점진적 폴백: leaf → leaf+parent → 대분류(cat1) prefix → top-N
                # 항상 최소 후보를 제공해야 AI가 빈 응답을 안 냄
                lines = []
                # 배치의 소싱 대분류(cat1) 모음 — 폴백 풀 구성용
                batch_cat1s: set[str] = set()
                for item in batch:
                    segs = [
                        s.strip() for s in item["leaf_path"].split(">") if s.strip()
                    ]
                    if segs:
                        batch_cat1s.add(segs[0])

                for m, cats in market_cat_lists.items():
                    # ESM 마켓은 특수 대분류 제외 적용
                    if m in ("gmarket", "auction"):
                        cats = [
                            c
                            for c in cats
                            if not _ai_filter_restricted(c.split(" > ")[0])
                        ]
                    # 키즈 SOFT 필터:
                    # - 배치 전체 성인 → 키즈 카테고리 hard 제거 (KC인증)
                    # - 배치 전체 키즈 → 키즈 우선이지만, 키즈 없으면 성인 fallback (해당 마켓에 키즈 트리 없는 경우 대비)
                    # - 혼합 → 그대로
                    if not batch_any_kids:
                        cats = [c for c in cats if not _is_kids(c)]
                    elif batch_all_kids:
                        kids_cats = [c for c in cats if _is_kids(c)]
                        # 키즈 카테고리가 충분히 있으면 키즈만, 없으면 전체 유지(프롬프트가 매핑 결정)
                        if len(kids_cats) >= 5:
                            cats = kids_cats
                    if not cats:
                        continue

                    # 성별 hard 필터: batch 단일 item일 때(batch_size=1), source category의
                    # 성별 키워드(남아/여아/남성/여성)와 반대 성별 카테고리 후보 풀에서 제거.
                    # 유아동 남아/여아/공용 카테고리 혼동 방지.
                    if batch_size == 1 and len(batch) == 1:
                        src_text = batch[0]["leaf_path"]
                        src_male = any(
                            kw in src_text for kw in ("남아", "남성", "맨즈")
                        )
                        src_female = any(
                            kw in src_text for kw in ("여아", "여성", "우먼")
                        )
                        if src_male and not src_female:
                            cats = [
                                c
                                for c in cats
                                if not any(kw in c for kw in ("여아", "여성"))
                            ]
                        elif src_female and not src_male:
                            cats = [
                                c
                                for c in cats
                                if not any(kw in c for kw in ("남아", "남성", "맨즈"))
                            ]

                    # 1단계: priority_kw(소싱 leaf + refs leaf)로만 점수 매칭 → 후보 60개 추출.
                    # — leaf_kw/parent_kw는 일반 키워드(남성/운동 등) noise 유발 → 1단계 점수에서 제외.
                    # — 60개로 확대해 정확한 카테고리(상하복세트/점퍼/축구복 등)도 후보에 포함되도록.
                    relevant: list[str] = []
                    if priority_kw:
                        scored = []
                        for c in cats:
                            score = sum(1 for kw in priority_kw if kw in c)
                            if score > 0:
                                scored.append((score, c))
                        scored.sort(key=lambda x: -x[0])
                        relevant = _gender_balanced_cap(
                            [c for _, c in scored], limit=60
                        )

                    # 2단계: priority 매칭 부족 시 leaf_kw 보강 (refs/source 둘 다 약한 케이스)
                    if len(relevant) < 10 and leaf_kw:
                        all_kw = leaf_kw | parent_kw
                        kw_matches = [
                            c
                            for c in cats
                            if c not in set(relevant) and any(kw in c for kw in all_kw)
                        ]
                        relevant = relevant + _gender_balanced_cap(
                            kw_matches, limit=60 - len(relevant)
                        )

                    # 3단계: 소싱 대분류(cat1) 키워드를 마켓 카테고리에 매핑 (의류↔패션, 신발↔패션잡화 등)
                    if not relevant:
                        # 대분류 동의어로 마켓 패션/뷰티/스포츠 대분류 매칭
                        broad_kws = set()
                        for c1 in batch_cat1s:
                            broad_kws.add(c1)
                            broad_kws.update(_split_kw(c1))
                        broad_kws = _expand_synonyms(broad_kws)
                        # 패션 키워드 fallback — 항상 추가
                        broad_kws.update(["패션", "의류", "잡화", "신발", "가방"])
                        broad_matches = [
                            c for c in cats if any(kw in c for kw in broad_kws)
                        ]
                        if broad_matches:
                            relevant = _gender_balanced_cap(broad_matches, limit=30)

                    # 4단계 최후 폴백: 마켓 카테고리 임의 30개 (AI에게 문맥이라도 제공)
                    if not relevant:
                        relevant = _gender_balanced_cap(cats[:200], limit=30)

                    if relevant:
                        lines.append(
                            f"- {market_labels.get(m, m)}:\n"
                            + "\n".join(f"  {c}" for c in relevant)
                        )
                        logger.info("[벌크매핑] %s 후보 풀: %d개", m, len(relevant))

                if lines:
                    cat_list_section = (
                        "\n[허용된 마켓 카테고리 — 이 중에서만 선택]\n"
                        + "\n".join(lines)
                        + "\n"
                    )
                    cat_rule = "각 마켓별로 위 목록에 있는 카테고리 문자열을 정확히 그대로 복사하여 선택. 목록에 없는 카테고리를 임의로 만들거나 변형 금지."
                else:
                    cat_list_section = ""
                    cat_rule = "각 마켓의 허용된 카테고리 중에서만 선택. 존재하지 않는 카테고리 생성 금지."

            # EXPORTED_RULES 기반 학습 예시 + DB 기존 매핑 few-shot 합산
            fewshot_block = (
                _build_fewshot_block(batch, target_markets) + _db_fewshot_global
            )

            prompt = f"""소싱 카테고리를 판매 마켓 카테고리에 매핑.

★최우선 규칙★
1. **소싱 카테고리의 마지막 segment(상품 유형)를 절대 변경 금지**. 예: 소싱이 "축구복"이면 11번가도 축구복/축구의류여야지, 트레이닝복으로 바꾸면 안됨. 소싱이 "상하복세트"면 상하복/세트여야지 단품 티셔츠로 바꾸면 안됨. 소싱이 "점퍼"면 점퍼/재킷이어야지 청재킷으로 바꾸면 안됨.
2. "기존매핑참고"가 있으면 그 다른 마켓 매핑의 **상품유형·성별·연령·세분화 깊이**를 따라가되, 1번 규칙(소싱 상품 유형 유지)이 우선이다.
예: 기존매핑참고가 "smartstore:패션의류 > 여성의류 > 티셔츠"이고 소싱이 "축구복"이면, refs 무시하고 11번가에서 축구복/축구의류 트리를 찾아라.
{fewshot_block}
{chr(10).join(cat_entries)}
{cat_list_section}
규칙:
- {cat_rule}
- 소싱 카테고리의 상품 유형(가방/신발/의류/스포츠 등)을 반드시 유지. 가방→가방, 신발→신발, 의류→의류로만 매핑.
- 성별 매칭 최우선: 항목에 "성별: 남성"이면 남성 카테고리만, "성별: 여성"이면 여성 카테고리만 선택.
- 소싱 카테고리 경로에 "남성", "맨즈", "남자" 단어가 있으면 반드시 남성 카테고리로 매핑.
- 소싱 카테고리 경로에 "여성", "우먼즈", "여자" 단어가 있으면 반드시 여성 카테고리로 매핑.
- 성별 근거가 전혀 없을 때만 남녀공용/성별무관 카테고리 선택 가능.
- 패션 상품(의류/신발/가방/액세서리)은 "패션의류"·"패션잡화" 대분류 우선. "스포츠/레저" 대분류는 소싱 카테고리에 "스포츠", "아웃도어", "골프", "등산", "런닝", "요가", "축구", "농구", "야구", "스키", "자전거" 등 스포츠 키워드가 있을 때만 선택.
- 연령 매칭 최우선: 항목 "연령: 키즈/주니어"면 키즈/주니어/아동/유아/베이비 카테고리로만 매핑. "연령: 성인"이면 주니어/아동/유아/베이비/키즈/kids/junior/baby 단어가 등장하는 카테고리 절대 금지 (KC인증). "기존매핑참고"의 다른 마켓 매핑이 키즈면 11번가도 키즈로, 성인이면 성인으로 매핑.
- 연령 동의어 매핑: "유아동"·"유아"·"아동"·"키즈"·"주니어"·"베이비"·"신생아"·"출산"은 모두 같은 연령군. 마켓별 표기가 달라도 동일 의미로 간주하여 매핑할 것. 예: 소싱이 "출산/유아동 > 신생아/유아의류 > 바지"이고 다른 마켓이 "유아동의류 > 바지"이면, 11번가에서는 "신생아의류" 또는 "키즈의류" 또는 "주니어의류" 트리에서 동일 의미를 골라라.
- 도서/음반/교재/학술 카테고리는 절대 선택 금지. 의류학 교재도 포함.
- 의류/패션과 무관한 카테고리(식품, 인테리어, 여행, 자동차, 반려동물 등)는 절대 선택 금지.
- 키워드 단순 매칭 금지. '웨이스트 백'은 허리에 차는 가방이지 바지가 아님. '기타'는 악기가 아닌 기타 등등을 의미함. 상품의 실제 의미를 파악하여 매핑.
- 학습 예시가 있으면 동일 대분류·동일 성별 패턴을 따라 매핑하세요.
- 기존매핑참고가 있으면 빈 문자열로 남기지 말고 반드시 동등한 의미의 카테고리를 골라라.
JSON만 응답:
{json.dumps({str(i + 1): {m: "" for m in target_markets} for i in range(len(batch))}, ensure_ascii=False)}"""

            # API 호출 (재시도 포함). 단건 호출(batch=1)은 max_tokens 축소.
            _max_tokens = 512 if batch_size == 1 else 2048
            for attempt in range(3):
                try:
                    response = await client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=_max_tokens,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    break
                except anthropic.RateLimitError:
                    if attempt < 2:
                        wait = 60 * (attempt + 1)
                        logger.warning(
                            "[벌크매핑] 429 rate limit — %d초 대기 (배치 %d/%d, 시도 %d/3)",
                            wait,
                            batch_start // batch_size + 1,
                            (len(items) + batch_size - 1) // batch_size,
                            attempt + 1,
                        )
                        await asyncio.sleep(wait)
                    else:
                        for _ in batch:
                            all_results.append("rate limit 초과")
                        continue

            try:
                text = response.content[0].text.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                    if text.endswith("```"):
                        text = text[:-3].strip()
                result = json.loads(text)

                target_set = set(target_markets)
                for idx in range(len(batch)):
                    key_str = str(idx + 1)
                    item_is_kids = batch_kids_flags[idx]
                    if key_str in result and isinstance(result[key_str], dict):
                        validated: Dict[str, str] = {}
                        for market, suggested in result[key_str].items():
                            if market in target_set and suggested:
                                # 패션 상품에 어울리지 않는 카테고리 접두어 차단
                                _fashion_exclude = (
                                    "인테리어소품",
                                    "식품",
                                    "출산/육아",
                                    "반려동물",
                                    "자동차용품",
                                    "도서/음반",
                                    "디지털/가전",
                                    "생활/건강",
                                    "스포츠/레저용품",
                                    "여행/숙박",
                                    "e쿠폰/티켓",
                                    "취미/컬렉션",
                                    "수입명품",
                                )
                                if any(
                                    suggested.startswith(p) for p in _fashion_exclude
                                ):
                                    logger.warning(
                                        f"[벌크매핑] '{suggested}' 패션 무관 카테고리 → 스킵"
                                    )
                                    continue
                                # 연령 매칭 검증:
                                # - 소싱이 성인 + 추천이 키즈 → 차단 (KC인증)
                                # - 소싱이 키즈 + 추천이 성인 → 차단 (잘못된 카테고리)
                                suggested_is_kids = _is_kids(suggested)
                                if not item_is_kids and suggested_is_kids:
                                    logger.warning(
                                        f"[벌크매핑] '{suggested}' 키즈 카테고리 (소싱은 성인) → 스킵 (KC인증)"
                                    )
                                    continue
                                if item_is_kids and not suggested_is_kids:
                                    logger.warning(
                                        f"[벌크매핑] '{suggested}' 성인 카테고리 (소싱은 키즈) → 스킵"
                                    )
                                    continue
                                # 대분류 단독 거부 — ' > ' 없으면 1단계 대분류
                                if " > " not in suggested:
                                    logger.warning(
                                        f"[벌크매핑] '{suggested}' 대분류 단독 → 스킵"
                                    )
                                    continue
                                # 동기화된 카테고리 목록에 있는지 검증
                                # 트리 미동기화(빈 목록) 시 AI 응답 무검증 통과 방지 — 유령 카테고리 차단
                                market_cat_list = market_cat_lists.get(market, [])
                                if not market_cat_list:
                                    logger.warning(
                                        f"[벌크매핑] {market} 카테고리 트리 미동기화 → '{suggested}' 스킵"
                                    )
                                    continue
                                if suggested in market_cat_list:
                                    validated[market] = suggested
                                else:
                                    # 유사매칭 시도
                                    fallback = _similarity_match_smartstore(
                                        suggested, market_cat_list
                                    )
                                    if fallback:
                                        logger.warning(
                                            f"[벌크매핑] AI '{suggested}' 목록에 없음 → {fallback}"
                                        )
                                        validated[market] = fallback
                                    else:
                                        logger.warning(
                                            f"[벌크매핑] AI '{suggested}' 목록에 없고 유사매칭 실패 → 스킵"
                                        )
                        all_results.append(validated)
                    else:
                        all_results.append("AI 응답에서 누락")
            except Exception as e:
                logger.error("[벌크매핑] 배치 응답 파싱 실패: %s", e)
                for _ in batch:
                    all_results.append(f"파싱 실패: {e}")

            # 배치 간 딜레이 (분당 토큰 제한 대응)
            if batch_start + batch_size < len(items):
                logger.info(
                    "[벌크매핑] 배치 %d/%d 완료, %d초 대기",
                    batch_start // batch_size + 1,
                    (len(items) + batch_size - 1) // batch_size,
                    batch_delay_sec,
                )
                await asyncio.sleep(batch_delay_sec)

        return all_results

    # ==================== AI Category Suggestion ====================

    async def ai_suggest_category(
        self,
        source_site: str,
        source_category: str,
        sample_products: List[str],
        sample_tags: Optional[List[str]] = None,
        target_markets: Optional[List[str]] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """카테고리 매핑 추천. 룰→유사도→AI 3단계.

        DB에 저장된 마켓 카테고리를 우선 사용하고, 없으면 하드코딩 fallback.
        """
        markets = target_markets or list(MARKET_CATEGORIES.keys())
        result: Dict[str, str] = {}

        # 0단계: DB 기존 매핑 직접 조회 — 저장된 값 있으면 AI 없이 그대로 반환
        existing = await self.mapping_repo.find_mapping(source_site, source_category)
        if existing and existing.target_mappings:
            for m in markets:
                val = (existing.target_mappings.get(m) or "").strip()
                if val and " > " in val:
                    result[m] = val
            remaining_after_db = [m for m in markets if m not in result]
            if not remaining_after_db:
                logger.info(
                    "[매핑-DB] %s > %s → 전 마켓 DB 캐시 히트",
                    source_site,
                    source_category,
                )
                return result
            logger.info(
                "[매핑-DB] %s > %s → %d/%d 마켓 DB 히트, 나머지 %s 계속",
                source_site,
                source_category,
                len(result),
                len(markets),
                remaining_after_db,
            )
            markets = remaining_after_db

        # 성별 감지 (상품명, 태그, 카테고리에서 추출)
        gender = _detect_gender(sample_products, sample_tags, source_category)

        # 1단계: 룰 기반 매핑 (모든 마켓)
        for m in markets:
            rule = _rule_match(source_site, source_category, m, gender)
            if rule is not None:
                result[m] = rule
                if rule:
                    logger.info(
                        f"[매핑-룰] {source_site} > {source_category} → {m}: {rule} (성별:{gender})"
                    )

        # 2단계: 유사도 매칭 (룰에서 못 찾은 마켓만) — 리프만 허용
        for m in markets:
            if m in result:
                continue
            cats = _filter_to_leaves(await self._get_market_categories(m))
            if cats:
                sim = _similarity_match_smartstore(source_category, cats)
                if sim:
                    result[m] = sim
                    logger.info(
                        f"[매핑-유사도] {source_site} > {source_category} → {m}: {sim}"
                    )

        # 1~2단계에서 모든 마켓 해결되면 AI 호출 불필요
        remaining_markets = [m for m in markets if m not in result]
        if not remaining_markets:
            return result

        # 3단계: AI 호출 (나머지 마켓만)
        from backend.core.config import settings

        key = api_key or settings.anthropic_api_key
        if not key:
            return result  # AI 키 없으면 1~2단계 결과만 반환

        import anthropic

        # DB 우선 조회 후 하드코딩 fallback (리프만 허용 — 비-리프 매핑 방지)
        # 소싱이 키즈인지 판별 — 키즈면 키즈 카테고리만, 성인이면 성인만
        _kids_keywords = (
            "주니어",
            "아동",
            "유아",
            "베이비",
            "키즈",
            "kids",
            "junior",
            "baby",
        )

        def _is_kids_text(text: str) -> bool:
            t = (text or "").lower()
            return any(kw in t for kw in _kids_keywords)

        source_is_kids = _is_kids_text(source_category) or any(
            _is_kids_text(n) for n in (sample_products or [])
        )
        market_cats: Dict[str, List[str]] = {}
        for m in remaining_markets:
            cats = _filter_to_leaves(await self._get_market_categories(m))
            if cats:
                if source_is_kids:
                    cats = [c for c in cats if _is_kids_text(c)]
                else:
                    cats = [c for c in cats if not _is_kids_text(c)]
                if cats:
                    market_cats[m] = cats

        if not market_cats:
            return {}

        # 키워드 추출 — leaf(하위) 키워드 우선, 상위는 보조
        cat_segments = [
            seg.strip() for seg in source_category.split(">") if seg.strip()
        ]
        # leaf 키워드: 마지막 세그먼트 + 태그 + 상품명 단어
        leaf_keywords: set[str] = set()
        if cat_segments:
            leaf_keywords.add(cat_segments[-1])
        for t in sample_tags or []:
            if t and not t.startswith("__") and len(t) >= 2:
                leaf_keywords.add(t)
        for name in sample_products[:3]:
            for word in name.split():
                if len(word) >= 2:
                    leaf_keywords.add(word)
        # 상위 키워드: 카테고리 상위 세그먼트
        parent_keywords = set(seg for seg in cat_segments[:-1] if len(seg) >= 2)

        # 동의어 확장
        leaf_keywords = _expand_synonyms(leaf_keywords)
        parent_keywords = _expand_synonyms(parent_keywords)

        # 필터: 키워드 매칭 개수로 가중치 정렬 — leaf(2점) + parent(1점)
        market_list_parts: list[str] = []
        for market, cats in market_cats.items():
            scored: list[tuple[int, str]] = []
            for c in cats:
                leaf_score = sum(2 for kw in leaf_keywords if kw in c)
                parent_score = sum(1 for kw in parent_keywords if kw in c)
                total = leaf_score + parent_score
                if total > 0:
                    scored.append((total, c))
            scored.sort(key=lambda x: -x[0])
            # 성별 균등 — 단건 AI 후보 풀도 한 성별 편중 방지
            relevant = _gender_balanced_cap([c for _, c in scored], limit=20)
            if not relevant:
                relevant = _gender_balanced_cap(cats, limit=10)
            market_list_parts.append(
                f"- {market}: {json.dumps(relevant, ensure_ascii=False)}"
            )
        market_list_str = "\n".join(market_list_parts)

        sample_str = ", ".join(sample_products[:3]) if sample_products else "(없음)"
        tag_str = ", ".join(
            [t for t in (sample_tags or []) if not t.startswith("__")][:5]
        )

        # 이미 매핑된 타 마켓 참고 정보 구성
        _ref_lines = ""
        if result:
            _ref_parts = [f"{mk}: {val}" for mk, val in result.items() if val]
            if _ref_parts:
                _ref_lines = (
                    "\n[이미 매핑된 타 마켓 — 참고용]\n"
                    + "\n".join(f"- {p}" for p in _ref_parts)
                    + "\n"
                )

        # 성별 레이블 (프롬프트 힌트용)
        _gender_label = {"male": "남성", "female": "여성", "unisex": "남녀공용"}.get(
            gender, "-"
        )

        # EXPORTED_RULES 기반 학습 예시 구성
        _single_fewshot = _build_fewshot_block(
            [{"site": source_site, "leaf_path": source_category}],
            list(market_cats.keys()),
        )
        # DB 기존 매핑 few-shot (실시간 반영)
        _db_fewshot = await self._get_db_fewshot_examples(
            [source_site],
            list(market_cats.keys()),
            exclude_cat=source_category,
        )

        prompt = f"""소싱 카테고리를 마켓 카테고리에 매핑.

[소싱] {source_site} | {source_category} | 상품: {sample_str} | 태그: {tag_str or "-"} | 성별: {_gender_label}
{_ref_lines}{_single_fewshot}{_db_fewshot}
[허용된 마켓 카테고리 — 이 중에서만 선택]
{market_list_str}

규칙:
1. 각 마켓별로 위 목록에 있는 카테고리 문자열을 정확히 그대로 복사하여 선택.
2. 목록에 없는 카테고리를 임의로 만들거나 변형하지 마세요.
3. 성별 매칭 최우선: 성별이 "남성"이면 남성 카테고리만, "여성"이면 여성 카테고리만 선택.
4. 소싱 카테고리 경로에 "남성", "맨즈", "남자" 단어가 있으면 반드시 남성 카테고리로 매핑.
5. 소싱 카테고리 경로에 "여성", "우먼즈", "여자" 단어가 있으면 반드시 여성 카테고리로 매핑.
6. 패션 상품(의류/신발/가방)은 "패션의류"·"패션잡화" 대분류 우선. "스포츠/레저"는 소싱에 스포츠 키워드가 있을 때만 선택.
7. 학습 예시가 있으면 동일 대분류·동일 성별 패턴을 따라 매핑하세요.
8. 확신이 없으면 빈 문자열("")로 남길 것. 억지로 맞지 않는 카테고리 선택 금지.
9. 대분류 단독 선택 절대 금지. 반드시 ' > '가 1개 이상 포함된 2단계 이상 경로만 선택. (예 불가: "패션의류", "스포츠/레저" / 예 가능: "패션의류 > 남성의류 > 티셔츠")
JSON만:
{json.dumps({m: "" for m in market_cats}, ensure_ascii=False)}"""

        logger.info(
            f"[AI매핑] 프롬프트 마켓: {list(market_cats.keys())} ({len(market_cats)}개)"
        )
        for mk, cats_list in market_cats.items():
            leaf_m = [c for c in cats_list if any(kw in c for kw in leaf_keywords)]
            logger.info(
                f"[AI매핑] {mk}: DB {len(cats_list)}개, 키워드매칭 {len(leaf_m)}개"
            )

        client = anthropic.AsyncAnthropic(api_key=key)

        # 429 rate limit 대비 재시도 (최대 3회, 60초 대기)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = await client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
                break
            except anthropic.RateLimitError as e:
                if attempt < max_retries - 1:
                    wait = 60 * (attempt + 1)  # 60초, 120초
                    logger.warning(
                        "Claude API 429 rate limit — %d초 후 재시도 (%d/%d)",
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Claude API rate limit 초과 (재시도 소진): %s", e)
                    raise ValueError(f"Claude API rate limit 초과: {e}") from e

        try:
            # 응답에서 JSON 추출
            text = response.content[0].text.strip()
            # ```json ... ``` 블록 제거
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3].strip()

            ai_result = json.loads(text)
            # AI 응답에서 누락된 마켓 확인
            missing = [m for m in market_cats if m not in ai_result]
            if missing:
                logger.warning(f"[AI매핑] AI 응답에서 누락된 마켓: {missing}")
            logger.info(f"[AI매핑] AI 응답 키: {list(ai_result.keys())}")

            # 응답 검증: 동기화된 카테고리 목록에 있는 것만 수용
            ai_validated: Dict[str, str] = {}
            for market, suggested in ai_result.items():
                if market not in market_cats or not suggested:
                    continue
                # 대분류 단독 거부 — ' > ' 없으면 1단계 대분류
                if " > " not in suggested:
                    logger.warning(
                        f"[AI매핑] {market}: '{suggested}' 대분류 단독 → 스킵"
                    )
                    continue
                if suggested in market_cats[market]:
                    ai_validated[market] = suggested
                    logger.info(f"[AI매핑] {market}: '{suggested}' ✓ 목록에 존재")
                else:
                    # 목록에 없으면 유사매칭 시도 — leaf 키워드 포함 후보 우선
                    fallback_pool = [
                        c
                        for c in market_cats[market]
                        if any(kw in c for kw in leaf_keywords)
                    ]
                    fallback = _similarity_match_smartstore(
                        suggested, fallback_pool or market_cats[market]
                    )
                    if fallback:
                        logger.warning(
                            f"[AI매핑] {market}: '{suggested}' 목록에 없음 → 유사매칭: {fallback}"
                        )
                        ai_validated[market] = fallback
                    else:
                        logger.warning(
                            f"[AI매핑] {market}: '{suggested}' 목록에 없고 유사매칭 실패 → 스킵"
                        )

            # 1~2단계 결과 + AI 검증 결과 병합 (AI로 보충)
            for k, v in ai_validated.items():
                if k not in result:
                    result[k] = v

            # AI가 빠뜨린 마켓은 상품명 키워드로 유사매칭 fallback
            for m in remaining_markets:
                if m not in result and m in market_cats:
                    # 상품명+태그 키워드로 직접 매칭
                    all_kw = leaf_keywords | parent_keywords
                    candidates = [
                        c for c in market_cats[m] if any(kw in c for kw in all_kw)
                    ]
                    if candidates:
                        best = max(
                            candidates, key=lambda c: sum(1 for kw in all_kw if kw in c)
                        )
                        result[m] = best
                        logger.info(f"[AI매핑] {m}: AI 누락 → 키워드 fallback: {best}")

            return result

        except json.JSONDecodeError as e:
            logger.error("AI 응답 JSON 파싱 실패: %s", e)
            return result  # AI 실패해도 1~2단계 결과는 반환
        except anthropic.APIError as e:
            logger.error("Claude API 오류: %s", e)
            return result  # AI 실패해도 1~2단계 결과는 반환

    # ==================== Bulk AI Mapping ====================

    async def bulk_ai_mapping(
        self,
        api_key: str,
        session: "AsyncSession",
        target_markets: Optional[List[str]] = None,
        source_site: Optional[str] = None,
        category_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """미매핑 카테고리 자동 매핑 + 기존 매핑 누락 마켓 보충.

        target_markets: 대상 마켓 (미지정 시 활성 계정 마켓)
        source_site: 소싱사이트 필터 (예: "MUSINSA")
        category_prefix: 카테고리 경로 prefix 필터 (예: "신발")
        """
        from sqlmodel import select
        from backend.domain.samba.collector.model import SambaCollectedProduct

        # MARKET_CATEGORIES에 없어도 DB 동기화로 카테고리가 존재하는 마켓 허용
        _db_only_markets = {"ssg_std"}
        _supported_markets = set(MARKET_CATEGORIES.keys()) | _db_only_markets

        if target_markets:
            # 사용자가 직접 선택한 마켓
            all_market_keys = set(target_markets) & _supported_markets
            logger.info(
                f"[벌크매핑] 사용자 선택 마켓: {all_market_keys} ({len(all_market_keys)}개)"
            )
        else:
            # 폴백: 활성 계정 마켓
            from backend.domain.samba.account.model import SambaMarketAccount

            acct_stmt = (
                select(SambaMarketAccount.market_type)
                .where(SambaMarketAccount.is_active == True)
                .distinct()
            )
            acct_result = await session.execute(acct_stmt)
            active_markets = {row[0] for row in acct_result.all()}
            if active_markets:
                all_market_keys = active_markets & _supported_markets
                logger.info(
                    f"[벌크매핑] 활성 마켓 대상: {all_market_keys} ({len(all_market_keys)}개)"
                )
            else:
                all_market_keys = _supported_markets

        if not all_market_keys:
            return {
                "mapped": 0,
                "updated": 0,
                "skipped": 0,
                "errors": ["대상 마켓이 없습니다"],
            }

        # 마켓별 동기화된 카테고리 목록 미리 로드 (검증용)
        all_market_cats: Dict[str, List[str]] = {}
        for mk in all_market_keys:
            cats = await self._get_market_categories(mk)
            if cats:
                all_market_cats[mk] = cats

        # 1) 수집 상품에서 고유 (site, leaf_category, 대표 상품명) 추출
        # OOM 방지: 필요한 컬럼만 조회 + source_site 필터 적용
        stmt = select(
            SambaCollectedProduct.source_site,
            SambaCollectedProduct.category,
            SambaCollectedProduct.category1,
            SambaCollectedProduct.category2,
            SambaCollectedProduct.category3,
            SambaCollectedProduct.category4,
            SambaCollectedProduct.name,
            SambaCollectedProduct.tags,
            SambaCollectedProduct.seo_keywords,
            SambaCollectedProduct.group_key,
        )
        if source_site:
            stmt = stmt.where(SambaCollectedProduct.source_site == source_site)
        result = await session.execute(stmt)
        products = result.all()

        # (site, leaf_path) → 태그 + SEO키워드 + 그룹명 + 성별
        cat_samples: Dict[tuple, List[str]] = {}
        cat_tags: Dict[tuple, List[str]] = {}
        cat_seo: Dict[tuple, List[str]] = {}
        cat_groups: Dict[tuple, set[str]] = {}
        cat_sex: Dict[tuple, set[str]] = {}  # p.sex 값 수집
        for p in products:
            site = p.source_site or ""
            if not site:
                continue
            cats = [p.category1, p.category2, p.category3, p.category4]
            cats = [c for c in cats if c]
            if not cats and p.category:
                cats = [c.strip() for c in p.category.split(">") if c.strip()]
            if not cats:
                continue
            leaf_path = " > ".join(cats)
            if category_prefix and not leaf_path.startswith(category_prefix):
                continue
            key = (site, leaf_path)
            if key not in cat_samples:
                cat_samples[key] = []
                # 태그
                tags = [
                    t
                    for t in (getattr(p, "tags", None) or [])
                    if t and not t.startswith("__")
                ]
                cat_tags[key] = tags[:10]
                # SEO 키워드
                cat_seo[key] = []
                # 그룹명
                cat_groups[key] = set()
                # 성별
                cat_sex[key] = set()
            # 상품명 수집 (성별 감지용, 최대 5개)
            if len(cat_samples[key]) < 5:
                cat_samples[key].append(p.name)
            # p.sex 수집 (남성/여성/남녀공용)
            if getattr(p, "sex", None):
                cat_sex[key].add(p.sex)
            # SEO 키워드 수집 (중복 제거)
            for kw in getattr(p, "seo_keywords", None) or []:
                if kw and kw not in cat_seo[key] and len(cat_seo[key]) < 10:
                    cat_seo[key].append(kw)
            # 그룹명 수집
            gk = getattr(p, "group_key", None)
            if gk and len(cat_groups[key]) < 3:
                cat_groups[key].add(gk)

        # 2) 기존 매핑 전체 조회
        from backend.domain.samba.category.model import SambaCategoryMapping

        existing_mappings = await self.mapping_repo.list_all()
        existing_map: Dict[tuple, SambaCategoryMapping] = {}
        for m in existing_mappings:
            existing_map[(m.source_site, m.source_category)] = m

        # 2-1) 매핑 테이블 항목 보충 — 수집상품(SambaCollectedProduct)이 없는 site도
        # 매핑 테이블에 mapped_refs(스스/롯데 등 기존 매핑)가 있으면 충분히 11번가 등을 추론 가능.
        # cat_samples에 빠진 (site, leaf_path)는 빈 sample/태그로라도 추가하여 AI 단계에서 처리.
        for em in existing_mappings:
            key = (em.source_site, em.source_category)
            # source_site/category_prefix 필터 적용
            if source_site and em.source_site != source_site:
                continue
            if category_prefix and not em.source_category.startswith(category_prefix):
                continue
            if key in cat_samples:
                continue
            # 매핑 테이블에 이미 있는데 cat_samples 없음 → mapped_refs 기반 보충
            cat_samples[key] = []  # sample 없음 — mapped_refs로 추론
            cat_tags[key] = []
            cat_seo[key] = []
            cat_groups[key] = set()
            cat_sex[key] = set()

        if not cat_samples:
            return {"mapped": 0, "updated": 0, "skipped": 0, "errors": []}

        mapped = 0
        updated = 0
        skipped = 0
        rule_mapped = 0
        similarity_mapped = 0
        errors: List[str] = []

        # DB 스마트스토어 카테고리 목록 (2단계 유사도 매칭용 — 리프만)
        ss_cats: list[str] = []
        if "smartstore" in all_market_keys:
            ss_cats = _filter_to_leaves(await self._get_market_categories("smartstore"))

        # ── 3단계 매핑 전략 ──
        # AI 호출 대상만 별도 수집
        batch_items: List[Dict[str, Any]] = []
        for (site, leaf_path), samples in cat_samples.items():
            existing = existing_map.get((site, leaf_path))
            current_targets = (existing.target_mappings or {}) if existing else {}
            missing_markets = all_market_keys - set(current_targets.keys())

            if not missing_markets:
                skipped += 1
                continue

            # 성별 감지: p.sex 값 우선, 없으면 상품명+태그+카테고리 기반 감지
            sex_values = cat_sex.get((site, leaf_path), set())
            if "여성" in sex_values and "남성" not in sex_values:
                gender = "female"
            elif "남성" in sex_values and "여성" not in sex_values:
                gender = "male"
            elif sex_values:
                gender = "unisex"
            else:
                tags_for_gender = cat_tags.get((site, leaf_path), [])
                gender = _detect_gender(samples, tags_for_gender, leaf_path)

            # ── 1단계: 룰 기반 매핑 (모든 마켓) ──
            resolved: Dict[str, str] = {}
            for mk in list(missing_markets):
                rule_result = _rule_match(site, leaf_path, mk, gender)
                if rule_result is not None:
                    resolved[mk] = rule_result
                    if rule_result:
                        logger.info(
                            f"[매핑-룰] {site} > {leaf_path} → {mk}: {rule_result} (성별:{gender})"
                        )

            # ── 2단계: 유사도 매칭 비활성화 ──
            # _similarity_match_smartstore는 키워드 표면 매칭으로 도메인 무관 카테고리 다수 생성.
            # 예: "원피스" → 완구/원피스피규어, "재킷" → 영아완구/점퍼루, "캡" → 단열에어캡 등.
            # 룰 미스 시 모두 3단계 AI에 위임하여 정확도 보장. (검증된 priority_kw 후보 풀 사용)

            # 1~2단계에서 해결된 마켓 저장
            if resolved:
                if existing:
                    new_targets = {**current_targets, **resolved}
                    try:
                        await self.update_mapping(
                            existing.id, {"target_mappings": new_targets}
                        )
                        updated += 1
                    except Exception as e:
                        errors.append(f"[저장실패] {site} > {leaf_path}: {e}")
                else:
                    try:
                        await self.create_mapping(
                            {
                                "source_site": site,
                                "source_category": leaf_path,
                                "target_mappings": resolved,
                            }
                        )
                        mapped += 1
                    except Exception as e:
                        errors.append(f"[저장실패] {site} > {leaf_path}: {e}")
                    # create 후 existing_map 갱신 (AI 단계에서 참조)
                    new_existing = await self.mapping_repo.find_by_async(
                        source_site=site, source_category=leaf_path
                    )
                    if new_existing:
                        existing = new_existing
                        existing_map[(site, leaf_path)] = new_existing
                        current_targets = new_existing.target_mappings or {}

                if resolved:
                    cnt = len(resolved)
                    rule_mapped += cnt
                    missing_markets -= set(resolved.keys())

            # ── 3단계: 나머지 마켓은 AI에 위임 ──
            if missing_markets:
                # AI에 SS 매핑 결과 전달 (ESM 정확도 향상용)
                ss_hint = current_targets.get("smartstore") or resolved.get(
                    "smartstore", ""
                )
                # 이미 매핑된 타 마켓 정보를 AI 참고용으로 전달
                _all_resolved = {**current_targets, **resolved}
                mapped_refs = {
                    mk: val
                    for mk, val in _all_resolved.items()
                    if val and mk not in missing_markets
                }
                batch_items.append(
                    {
                        "site": site,
                        "leaf_path": leaf_path,
                        "samples": samples,
                        "tags": cat_tags.get((site, leaf_path), []),
                        "seo": cat_seo.get((site, leaf_path), []),
                        "groups": list(cat_groups.get((site, leaf_path), set())),
                        "gender": gender,
                        "ss_mapped": ss_hint,
                        "mapped_refs": mapped_refs,
                        "target_markets": list(missing_markets),
                        "existing": existing,
                        "mode": "update" if existing else "create",
                    }
                )

        logger.info(
            f"[벌크매핑] 1~2단계 완료: 룰/유사도={rule_mapped}건, AI대상={len(batch_items)}건, 스킵={skipped}건"
        )

        if not batch_items:
            return {
                "mapped": mapped,
                "updated": updated,
                "skipped": skipped,
                "rule_mapped": rule_mapped,
                "errors": errors,
            }

        def _ai_fallback_for_item(
            item: Dict[str, Any], ai_result: Dict[str, str]
        ) -> Dict[str, str]:
            """AI가 빈 응답한 마켓을 mapped_refs/leaf_path 키워드로 결정론적 매칭.

            AI가 ""만 반환한 마켓에 대해, 다음 우선순위로 11번가 등 후보 검색:
            1. mapped_refs(스스/롯데ON 매핑) 경로의 leaf 키워드로 유사매칭
            2. 소싱 leaf_path 키워드로 유사매칭
            연령(키즈/성인) 미스매치 카테고리는 제외.
            """
            site = item["site"]
            leaf_path = item["leaf_path"]
            mapped_refs = item.get("mapped_refs") or {}
            target_markets_item = item.get("target_markets") or []
            item_is_kids = any(
                kw in leaf_path.lower() for kw in _kids_keywords_const
            ) or any(
                any(kw in v.lower() for kw in _kids_keywords_const)
                for v in mapped_refs.values()
            )

            # 키워드 후보군 구성: mapped_refs 경로 leaf + 소싱 leaf
            ref_keywords: set[str] = set()
            for v in mapped_refs.values():
                if v and " > " in v:
                    last_seg = v.split(" > ")[-1]
                    for part in last_seg.replace("/", " ").split():
                        if len(part) >= 2:
                            ref_keywords.add(part)
            src_segs = [s.strip() for s in leaf_path.split(">") if s.strip()]
            if src_segs:
                for part in src_segs[-1].replace("/", " ").split():
                    if len(part) >= 2:
                        ref_keywords.add(part)

            # AI 폴백 결과에서 절대 선택 금지할 패션 무관 카테고리 prefix
            # — _batch_ai_suggest 응답 검증과 동일 로직을 폴백에도 적용해 도서/완구/생활용품 등으로 잘못 매핑 방지.
            _fallback_exclude_prefixes = (
                "인테리어",
                "식품",
                "완구",
                "출산/육아 > 기저귀",
                "도서",
                "음반",
                "생활용품 > 보수",
                "디지털/가전",
                "여행",
                "취미",
                "수입명품",
                "자동차",
                "반려동물",
                "심판용품",
                "응원용품",
            )
            # 패션 무관 키워드 (카테고리 어디든 포함되면 차단)
            _fallback_exclude_keywords = (
                "기저귀가방",
                "단열에어캡",
                "점퍼루",
                "원피스피규어",
                "랫풀다운",
                "파티코스튬",
                "할로윈",
                "응원용품",
                "심판",
                "기타스포츠화",
            )

            def _is_safe_candidate(c: str) -> bool:
                if any(c.startswith(p) for p in _fallback_exclude_prefixes):
                    return False
                low = c
                if any(kw in low for kw in _fallback_exclude_keywords):
                    return False
                return True

            patched: Dict[str, str] = dict(ai_result) if ai_result else {}
            for mk in target_markets_item:
                if patched.get(mk):
                    continue
                cats = all_market_cats.get(mk, [])
                if not cats:
                    continue
                # 패션 무관 카테고리 1차 차단
                cats = [c for c in cats if _is_safe_candidate(c)]
                if not cats:
                    continue
                # 연령 매칭
                cands = [
                    c
                    for c in cats
                    if (
                        any(kw in c.lower() for kw in _kids_keywords_const)
                        == item_is_kids
                    )
                ]
                if not cands:
                    cands = cats
                # 키워드 점수 매칭 — 최소 점수 2 이상만 신뢰 (1점은 노이즈 매칭일 가능성 큼)
                if ref_keywords:
                    scored = [
                        (sum(1 for kw in ref_keywords if kw in c), c) for c in cands
                    ]
                    scored = [s for s in scored if s[0] >= 2]
                    if scored:
                        scored.sort(key=lambda x: -x[0])
                        best = scored[0][1]
                        patched[mk] = best
                        logger.info(
                            "[벌크매핑-AI폴백] %s > %s → %s: %s (refs=%d, score=%d)",
                            site,
                            leaf_path,
                            mk,
                            best,
                            len(mapped_refs),
                            scored[0][0],
                        )
                    else:
                        logger.warning(
                            "[벌크매핑-AI폴백] %s > %s → %s: 점수 1점 이하만 매칭 → 스킵",
                            site,
                            leaf_path,
                            mk,
                        )
            return patched

        _kids_keywords_const = (
            "주니어",
            "아동",
            "유아",
            "베이비",
            "키즈",
            "kids",
            "junior",
            "baby",
        )

        # 배치 AI 호출 + 빈 결과 재시도 (최대 2회)
        remaining_items = batch_items
        for round_num in range(2):
            if not remaining_items:
                break

            batch_results = await self._batch_ai_suggest(
                remaining_items,
                list(all_market_keys),
                api_key,
            )

            retry_items: List[Dict[str, Any]] = []

            for item, ai_result in zip(remaining_items, batch_results):
                site = item["site"]
                leaf_path = item["leaf_path"]

                if isinstance(ai_result, str):
                    if round_num == 0:
                        retry_items.append(item)
                        logger.warning(
                            f"[벌크매핑] 에러 → 재시도 대기: {site} > {leaf_path}: {ai_result}"
                        )
                    else:
                        # 2회 실패한 경우라도 AI 폴백 시도
                        ai_result = _ai_fallback_for_item(item, {})
                        if not ai_result:
                            errors.append(
                                f"[{item['mode']}] {site} > {leaf_path}: AI 폴백 실패"
                            )
                            continue
                    if isinstance(ai_result, str):
                        continue

                # 2라운드에서 AI 빈 응답 → mapped_refs 폴백 적용
                if round_num == 1 and isinstance(ai_result, dict):
                    ai_result = _ai_fallback_for_item(item, ai_result)

                if item["mode"] == "update":
                    existing = item["existing"]
                    # DB에서 최신 target_mappings 다시 로드 (1~2단계 결과 반영)
                    refreshed = await self.mapping_repo.get_async(existing.id)
                    current_targets = (
                        refreshed.target_mappings
                        if refreshed
                        else existing.target_mappings
                    ) or {}
                    new_targets = {**current_targets}
                    for market, cat in ai_result.items():
                        if cat:
                            new_targets[market] = cat
                    # 새로 추가된 마켓이 없으면 빈 결과
                    if new_targets == current_targets:
                        if round_num == 0:
                            retry_items.append(item)
                        else:
                            errors.append(f"[보충] {site} > {leaf_path}: AI 빈 응답")
                        continue
                    try:
                        await self.update_mapping(
                            existing.id, {"target_mappings": new_targets}
                        )
                        updated += 1
                    except Exception as e:
                        errors.append(f"[보충] {site} > {leaf_path}: {e}")
                else:
                    target_mappings = {m: c for m, c in ai_result.items() if c}
                    if target_mappings:
                        try:
                            await self.create_mapping(
                                {
                                    "source_site": site,
                                    "source_category": leaf_path,
                                    "target_mappings": target_mappings,
                                }
                            )
                            mapped += 1
                        except Exception as e:
                            errors.append(f"[신규] {site} > {leaf_path}: {e}")
                    else:
                        if round_num == 0:
                            retry_items.append(item)
                        else:
                            errors.append(
                                f"[신규] {site} > {leaf_path}: AI 빈 응답 (2회 실패)"
                            )

            remaining_items = retry_items
            if retry_items and round_num == 0:
                logger.info(f"[벌크매핑] {len(retry_items)}개 빈 결과 재시도")
                await asyncio.sleep(3)

        # ── ESM 크로스매핑 자동 적용 ──
        # 지마켓/옥션 중 하나만 매핑된 경우 반대쪽 자동 복사
        esm_pair = {"gmarket", "auction"}
        if esm_pair & all_market_keys:
            esm_copied = 0
            for from_mk, to_mk in [("gmarket", "auction"), ("auction", "gmarket")]:
                if from_mk in all_market_keys and to_mk in all_market_keys:
                    try:
                        cross_result = await self.copy_esm_cross_mapping(
                            from_market=from_mk,
                            to_market=to_mk,
                        )
                        esm_copied += cross_result.get("copied", 0)
                    except Exception as e:
                        logger.warning("[벌크매핑] ESM 크로스매핑 실패: %s", e)
            if esm_copied:
                logger.info("[벌크매핑] ESM 크로스매핑 자동 적용: %d건", esm_copied)
                updated += esm_copied

        # 벌크매핑 완료 후 rules_exported.py 자동 갱신
        if mapped + updated > 0:
            asyncio.create_task(self._rebuild_exported_rules())

        return {
            "mapped": mapped,
            "updated": updated,
            "skipped": skipped,
            "rule_mapped": rule_mapped,
            "errors": errors,
        }
