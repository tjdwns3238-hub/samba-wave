"""가디 계정 쿠팡 DENIED 상품 재등록 v2 — 이미지 정규화 포함.

이미지 정규화(ImageTransformService) 추가로 사이즈 반려 방지.
"""

import asyncio
import json
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from backend.core.config import settings
from backend.domain.samba.proxy.coupang import CoupangClient

ACCOUNT_ID = "ma_01KNZV0ZWXW52W0G4TYG3AJH9Q"
LOTTE_CODE = "HYUNDAI"
_SERVER_KEYS = {
    "sellerProductId",
    "productId",
    "approvalStatus",
    "statusName",
    "exposedStatusName",
    "createdAt",
    "updatedAt",
}
_ITEM_SERVER_KEYS = {"vendorItemId", "itemId"}

# 쿠팡 이미지 검증 사양
_IMG_KW = dict(
    max_bytes=10 * 1024 * 1024,
    max_dim=5000,
    min_dim=500,
    enforce_max_dim=True,
)


def _make_engine():
    url = (
        f"postgresql+asyncpg://{settings.write_db_user}:{settings.write_db_password}"
        f"@{settings.write_db_host}:{settings.write_db_port}/{settings.write_db_name}"
    )
    kw = {"ssl": "require"} if settings.use_db_ssl else {}
    return create_async_engine(url, connect_args=kw, pool_size=3, max_overflow=2)


async def get_pg_conn():
    return await asyncpg.connect(
        host=settings.write_db_host,
        port=settings.write_db_port,
        user=settings.write_db_user,
        password=settings.write_db_password,
        database=settings.write_db_name,
        ssl="require" if settings.use_db_ssl else None,
    )


def strip_server_ids(data: dict) -> dict:
    data = {k: v for k, v in data.items() if k not in _SERVER_KEYS}
    items = data.get("items")
    if isinstance(items, list):
        data["items"] = [
            {k: v for k, v in it.items() if k not in _ITEM_SERVER_KEYS}
            if isinstance(it, dict)
            else it
            for it in items
        ]
    return data


def inject_model_no(data: dict, style_code: str) -> dict:
    items = data.get("items")
    if not isinstance(items, list):
        return data
    new_items = []
    for item in items:
        if not isinstance(item, dict):
            new_items.append(item)
            continue
        item = dict(item)
        if style_code:
            item["modelNo"] = style_code[:50]
        barcode = item.get("barcode") or ""
        item["barcode"] = barcode
        item["emptyBarcode"] = not barcode
        if item["emptyBarcode"]:
            item["emptyBarcodeReason"] = (
                "품번(MPN)으로 대체" if style_code else "바코드 없음"
            )
        new_items.append(item)
    data["items"] = new_items
    return data


def clean_url(url: str) -> str:
    """URL에서 공백/줄바꿈 문자 제거."""
    return url.strip().replace("\n", "").replace("\r", "").replace(" ", "%20")


async def normalize_images(svc, post_data: dict) -> dict:
    """items[*].images 이미지 정규화 (R2 미러링)."""
    from backend.domain.samba.image.service import ImageTransformService

    if not isinstance(svc, ImageTransformService):
        return post_data

    items = post_data.get("items") or []
    new_items = []
    for item in items:
        if not isinstance(item, dict):
            new_items.append(item)
            continue
        item = dict(item)
        imgs = item.get("images") or []
        if imgs:
            # URL 정리
            urls = [
                clean_url(i.get("vendorPath", "")) for i in imgs if isinstance(i, dict)
            ]
            urls = [u for u in urls if u]
            try:
                fixed, _, _ = await svc.mirror_oversized_to_r2(urls, **_IMG_KW)
                for idx, img in enumerate(imgs):
                    if isinstance(img, dict) and idx < len(fixed):
                        img = dict(img)
                        img["vendorPath"] = fixed[idx]
                imgs = [
                    dict(i, vendorPath=f)
                    if isinstance(i, dict) and idx < len(fixed)
                    else i
                    for idx, (i, f) in enumerate(zip(imgs, fixed))
                ]
                item["images"] = imgs
            except Exception as e:
                print(f"    이미지 정규화 실패(원본유지): {e}")
        new_items.append(item)
    post_data["items"] = new_items
    return post_data


async def update_db(conn, old_spid: str, new_spid: str) -> int:
    result = await conn.execute(
        """
        UPDATE samba_collected_product
        SET market_product_nos = jsonb_set(
            COALESCE(market_product_nos, '{}'),
            ARRAY[$1],
            to_jsonb(CAST($2 AS text))
        )
        WHERE market_product_nos ->> $1 = $3
        """,
        ACCOUNT_ID,
        new_spid,
        old_spid,
    )
    return int(result.split()[-1]) if result else 0


async def main() -> None:
    # asyncpg 직접 연결 (메타데이터용)
    pg = await get_pg_conn()
    acct = await pg.fetchrow(
        "SELECT account_label, additional_fields FROM samba_market_account WHERE id = $1",
        ACCOUNT_ID,
    )

    # 삼바 DB 매핑 (style_code, brand)
    raw = acct["additional_fields"] or {}
    extras = json.loads(raw) if isinstance(raw, str) else (raw or {})
    access_key = extras.get("accessKey") or ""
    secret_key = extras.get("secretKey") or ""
    vendor_id = extras.get("vendorId") or ""
    print(f"▶ 계정: {acct['account_label']}")

    client = CoupangClient(access_key, secret_key, vendor_id)

    print("DENIED 목록 조회 중...")
    denied = await client.list_seller_products(status="DENIED")
    print(f"DENIED: {len(denied):,}개")

    spid_list = [d["seller_product_id"] for d in denied]
    samba_map: dict[str, tuple[str, str]] = {}
    for i in range(0, len(spid_list), 100):
        batch = spid_list[i : i + 100]
        rows = await pg.fetch(
            "SELECT market_product_nos ->> $1 AS spid, style_code, brand "
            "FROM samba_collected_product WHERE market_product_nos ->> $1 = ANY($2)",
            ACCOUNT_ID,
            batch,
        )
        for r in rows:
            samba_map[r["spid"]] = (
                (r["style_code"] or "").strip(),
                (r["brand"] or "").strip(),
            )
    await pg.close()
    print(f"삼바 DB 매핑: {len(samba_map):,}개")

    # SQLAlchemy session — ImageTransformService용
    engine = _make_engine()
    AsyncSessionLocal = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    ok, fail = 0, 0
    fail_log = []

    async with AsyncSessionLocal() as session:
        from backend.domain.samba.image.service import ImageTransformService

        img_svc = ImageTransformService(session)

        for i, item in enumerate(denied, 1):
            spid = item["seller_product_id"]
            name = item["product_name"][:40]
            style_code, brand = samba_map.get(spid, ("", ""))

            try:
                resp = await client.get_product(spid)
                data = (
                    resp.get("data", resp)
                    if isinstance(resp, dict) and "data" in resp
                    else resp
                )
                if not isinstance(data, dict):
                    fail += 1
                    continue

                current_code = data.get("deliveryCompanyCode", "")

                post_data = strip_server_ids(data)
                post_data["deliveryCompanyCode"] = LOTTE_CODE
                post_data["vendorId"] = vendor_id
                post_data["vendorUserId"] = vendor_id
                post_data["requested"] = True
                post_data = inject_model_no(post_data, style_code)

                # 이미지 정규화
                post_data = await normalize_images(img_svc, post_data)

                # brandId
                brand_name = brand or (data.get("brand") or "")
                if brand_name and not post_data.get("brandId"):
                    try:
                        bid = await client.search_brand_id(brand_name)
                        if bid:
                            post_data["brandId"] = bid
                    except Exception:
                        pass

                await client.delete_product(spid)
                await asyncio.sleep(0.3)

                reg = await client.register_product(post_data)
                new_spid = ""
                if isinstance(reg, dict):
                    inner = reg.get("data", {})
                    if isinstance(inner, dict):
                        new_spid = str(inner.get("data", "") or "")
                    elif inner:
                        new_spid = str(inner)

                if not new_spid or not new_spid.isdigit():
                    err = str(reg)[:150]
                    fail += 1
                    fail_log.append(f"{spid}: {err}")
                    print(f"  ✗ {spid} 재등록실패: {err}")
                    if "초과하였습니다" in err or "오늘 등록할 수 있는" in err:
                        print(f"\n  ⚠️  한도 초과. 중단. 성공:{ok:,} 실패:{fail:,}")
                        break
                    continue

                pg2 = await get_pg_conn()
                updated = await update_db(pg2, spid, new_spid)
                await pg2.close()

                ok += 1
                print(
                    f"  ✓ [{i:,}/{len(denied):,}] {spid}→{new_spid} [{current_code}→{LOTTE_CODE}] DB:{updated} ({name[:30]})"
                )
                await asyncio.sleep(0.5)

            except Exception as e:
                fail += 1
                fail_log.append(f"{spid}: {e}")
                print(f"  ✗ {spid} — {e}")

            if i % 50 == 0:
                print(f"  [{i:,}/{len(denied):,}] ok={ok:,} fail={fail:,}")

    await engine.dispose()

    print("\n  ═══════════════════════════════════════")
    print(f"  완료: 성공 {ok:,} / 실패 {fail:,}")
    if fail_log:
        print(f"\n  실패 ({len(fail_log)}개):")
        for f in fail_log[:20]:
            print(f"    {f}")


if __name__ == "__main__":
    asyncio.run(main())
