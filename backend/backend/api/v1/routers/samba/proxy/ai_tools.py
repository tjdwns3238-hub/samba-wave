"""AI 모델 테스트 엔드포인트 (Claude, Gemini, R2, fal.ai)."""

from __future__ import annotations

from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Header, Query, UploadFile
from fastapi.responses import Response
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.utils.logger import logger

from ._helpers import _get_setting, _set_setting

router = APIRouter(tags=["samba-proxy"])

# JWT 인증 없이 워커 토큰만으로 접근하는 bg-jobs 전용 라우터
bg_worker_router = APIRouter(prefix="/proxy", tags=["samba-proxy-worker"])


def _default_tenant_id() -> str | None:
    """BG_WORKER_TENANT_ID 환경변수에서 테넌트 ID 조회.

    멀티테넌트 환경에서 설정이 '{tenant_id}:{key}' 형식으로 저장된 경우
    환경변수로 테넌트 ID를 지정하면 올바른 키로 조회한다.
    미설정 시 None 반환 → 기존 단일테넌트 동작 그대로.
    """
    import os

    val = os.environ.get("BG_WORKER_TENANT_ID", "").strip()
    return val if val else None


# ═══════════════════════════════════════════════
# Claude AI API 인증 테스트
# ═══════════════════════════════════════════════


@router.post("/claude/test")
async def claude_api_test(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """Claude API 키 유효성 검증 — 최소 메시지 전송 테스트."""
    creds = await _get_setting(session, "claude", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "Claude API 설정이 저장되지 않았습니다."}

    api_key = creds.get("apiKey", "")
    model = creds.get("model", "claude-sonnet-4-6")
    if not api_key:
        return {"success": False, "message": "API Key가 비어있습니다."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 5,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                used_model = data.get("model", model)
                return {
                    "success": True,
                    "message": f"인증 성공 (모델: {used_model})",
                }
            else:
                err = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                err_msg = (
                    err.get("error", {}).get("message", "")
                    if isinstance(err.get("error"), dict)
                    else str(err.get("error", ""))
                )
                return {
                    "success": False,
                    "message": err_msg or f"HTTP {resp.status_code}",
                }
    except Exception as exc:
        logger.error(f"[Claude] API 테스트 실패: {exc}")
        return {"success": False, "message": f"API 호출 실패: {exc}"}


@router.post("/gemini/test")
async def gemini_api_test(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """Gemini API 키 유효성 검증."""
    creds = await _get_setting(session, "gemini", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "Gemini API 설정이 저장되지 않았습니다."}

    api_key = creds.get("apiKey", "")
    model = creds.get("model", "gemini-2.5-flash")
    if not api_key:
        return {"success": False, "message": "API Key가 비어있습니다."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                json={
                    "contents": [{"parts": [{"text": "hi"}]}],
                    "generationConfig": {"maxOutputTokens": 5},
                },
            )
            if resp.status_code == 200:
                return {"success": True, "message": f"인증 성공 (모델: {model})"}
            else:
                err = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                err_msg = (
                    err.get("error", {}).get("message", "")
                    if isinstance(err.get("error"), dict)
                    else str(err.get("error", ""))
                )
                return {
                    "success": False,
                    "message": err_msg or f"HTTP {resp.status_code}",
                }
    except Exception as exc:
        logger.error(f"[Gemini] API 테스트 실패: {exc}")
        return {"success": False, "message": f"API 호출 실패: {exc}"}


@router.post("/r2/test")
async def r2_test(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """Cloudflare R2 연결 테스트."""
    creds = await _get_setting(session, "cloudflare_r2", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "R2 settings not found"}

    account_id = str(creds.get("accountId", "")).strip()
    access_key = str(creds.get("accessKey", "")).strip()
    secret_key = str(creds.get("secretKey", "")).strip()
    bucket_name = str(creds.get("bucketName", "")).strip()

    if not access_key or not secret_key or not bucket_name:
        return {
            "success": False,
            "message": "Access Key, Secret Key, Bucket Name required",
        }

    try:
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        s3.head_bucket(Bucket=bucket_name)
        return {"success": True, "message": f"R2 connected (bucket: {bucket_name})"}
    except Exception as exc:
        logger.error(f"[R2] test failed: {exc}")
        return {"success": False, "message": f"R2 connection failed: {str(exc)[:200]}"}


@router.post("/r2/upload-image")
async def r2_upload_image(
    filename: str = Query(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """브라우저 WASM 배경 제거 결과 이미지를 R2에 업로드."""
    creds = await _get_setting(session, "cloudflare_r2", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "R2 설정이 저장되지 않았습니다"}

    account_id = str(creds.get("accountId", "")).strip()
    access_key = str(creds.get("accessKey", "")).strip()
    secret_key = str(creds.get("secretKey", "")).strip()
    bucket_name = str(creds.get("bucketName", "")).strip()
    public_url_base = str(creds.get("publicUrl", "")).strip().rstrip("/")

    if not access_key or not secret_key or not bucket_name:
        return {
            "success": False,
            "message": "R2 설정 불완전 (Access Key, Secret Key, Bucket Name 필요)",
        }

    try:
        import boto3

        content = await file.read()
        s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )
        key = f"transformed/{filename}"
        s3.put_object(
            Bucket=bucket_name, Key=key, Body=content, ContentType="image/webp"
        )
        return {"success": True, "public_url": f"{public_url_base}/{key}"}
    except Exception as exc:
        logger.error(f"[R2] upload-image 실패: {exc}")
        return {"success": False, "message": str(exc)[:200]}


# 소싱사이트 이미지 CDN 허용 도메인 — 서버사이드 SSRF 방어
_IMAGE_FETCH_ALLOWED_HOSTS: frozenset[str] = frozenset(
    [
        # 무신사
        "image.msscdn.net",
        "cdn.musinsa.com",
        # 롯데ON
        "thumbnail6.lotteon.com",
        "thumbnail.lotteon.com",
        # GS샵
        "www.gsshop.com",
        "image.gsshop.com",
        # ABCmart / GrandStage
        "img.a-rt.com",
        "image.a-rt.com",
        # SSG
        "static.ssgcdn.com",
        "img.ssgcdn.com",
        # KREAM
        "kream-product.clutch.io",
        "cdn.kream.co.kr",
        # 패션플러스
        "img.fashionplus.co.kr",
        "www.fashionplus.co.kr",
        # Nike
        "static.nike.com",
        "n.neuralmagic.com",
        # Adidas
        "assets.adidas.com",
        # 롯데홈쇼핑
        "image.lotteimall.com",
        # 기타 공통 CDN
        "cdn.jsdelivr.net",
    ]
)


def _is_image_fetch_allowed(url: str) -> bool:
    """SSRF 방어 — 허용된 소싱사이트 호스트이고 userinfo 없는지 확인."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # userinfo(user:pass@host) 차단
        if parsed.username or parsed.password:
            return False
        # https 또는 http만 허용 (file://, ftp:// 등 차단)
        if parsed.scheme not in ("https", "http"):
            return False
        host = (parsed.hostname or "").lower()
        # 정확한 호스트 또는 서브도메인 매칭
        if any(host == h or host.endswith(f".{h}") for h in _IMAGE_FETCH_ALLOWED_HOSTS):
            return True
        # Cloudflare R2 퍼블릭 버킷: pub-<hash>.r2.dev 형식
        if host.startswith("pub-") and host.endswith(".r2.dev"):
            return True
        return False
    except Exception:
        return False


@router.get("/image-fetch")
async def image_fetch_proxy(url: str = Query(...)) -> Response:
    """외부 이미지 URL을 서버에서 가져와 반환 (브라우저 CORS 우회).

    SSRF 방어: 허용된 소싱사이트 CDN 호스트만 요청 가능.
    """
    if len(url) > 2000:
        return Response(status_code=400, content=b"URL too long")
    if not _is_image_fetch_allowed(url):
        logger.warning(f"[image-fetch] 차단된 URL: {url[:100]}")
        return Response(status_code=403, content=b"Host not allowed")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; SambaWave/1.0)"},
            )
            content = resp.content
            if len(content) > 20 * 1024 * 1024:
                return Response(status_code=413, content=b"Image too large")
            content_type = resp.headers.get("content-type", "image/jpeg")
            return Response(content=content, media_type=content_type)
    except Exception as exc:
        logger.error(f"[image-fetch] 실패: {url[:100]} — {exc}")
        return Response(status_code=502, content=b"Fetch failed")


@router.get("/fal/status")
async def fal_ai_status(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """fal.ai 계정 상태 확인 (잔액 부족 여부)."""
    creds = await _get_setting(session, "fal_ai", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"status": "no_key", "message": "API 키 미등록"}

    api_key = str(creds.get("apiKey", "")).strip()
    if not api_key:
        return {"status": "no_key", "message": "API 키 비어있음"}

    import os

    os.environ["FAL_KEY"] = api_key
    try:
        import fal_client

        # 최소 비용 호출로 계정 상태 확인 (실제 이미지 생성 없이 큐 제출만)
        handle = await fal_client.submit_async(
            "fal-ai/flux/dev",
            arguments={
                "prompt": "test",
                "num_inference_steps": 1,
                "image_size": "square_hd",
            },
        )
        # 큐 제출 성공 → 잔액 있음. 즉시 취소
        await fal_client.cancel_async("fal-ai/flux/dev", handle.request_id)
        return {"status": "ok", "message": "사용 가능"}
    except Exception as e:
        err = str(e)
        if "Exhausted balance" in err or "locked" in err.lower():
            return {"status": "no_balance", "message": "잔액 부족"}
        if "401" in err or "unauthorized" in err.lower():
            return {"status": "invalid_key", "message": "API 키 무효"}
        return {"status": "error", "message": err[:100]}


@router.post("/images/transform")
async def transform_images(
    request: dict[str, Any],
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """AI 이미지 변환 — background 모드는 로컬 워커 큐에 등록, 나머지는 Cloud Run 처리."""
    from backend.domain.samba.image.service import ImageTransformService

    svc = ImageTransformService(session)
    product_ids = request.get("product_ids", [])
    group_ids = request.get("group_ids", [])
    scope = request.get(
        "scope", {"thumbnail": True, "additional": False, "detail": False}
    )
    mode = request.get("mode", "background")  # background | scene | model
    model_preset = request.get("model_preset", "female_v1")

    # 그룹 ID로 요청 시 해당 그룹의 상품 ID 조회
    if group_ids and not product_ids:
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )

        repo = SambaCollectedProductRepository(session)
        for gid in group_ids:
            products = await repo.list_by_filter(gid, skip=0, limit=10000)
            product_ids.extend([p.id for p in products])
        product_ids = list(set(product_ids))

    if not product_ids:
        return {"success": False, "message": "No products selected"}

    # 배경제거는 로컬 워커 큐에 등록 (Cloud Run에서 처리 안 함)
    if mode == "background":
        from backend.domain.samba.job.model import SambaJob

        job = SambaJob(
            job_type="bg_remove",
            status="pending",
            payload={"product_ids": product_ids, "scope": scope},
            total=len(product_ids),
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        logger.info(
            f"[배경제거] 로컬 워커 큐 등록: job_id={job.id}, {len(product_ids)}개 상품"
        )
        return {
            "success": True,
            "status": "queued",
            "job_id": job.id,
            "message": f"로컬 워커 큐 등록 완료 ({len(product_ids)}개 상품)",
            "total_transformed": 0,
            "total_failed": 0,
        }

    try:
        result = await svc.transform_products(product_ids, scope, mode, model_preset)
        transformed = result.get("total_transformed", 0)
        return {"success": transformed > 0, **result}
    except Exception as exc:
        logger.error(f"[이미지변환] transform failed: {exc}")
        return {"success": False, "message": str(exc)[:300]}


async def _verify_worker_token(
    token: str, session: AsyncSession, tenant_id: str | None = None
) -> bool:
    """X-Worker-Token 검증 — 환경변수 BG_WORKER_TOKEN 또는 DB bg_worker.worker_token과 비교."""
    import os

    if not token:
        return False
    # docker-compose 내부 통신용: 환경변수 직접 매칭
    env_token = os.environ.get("BG_WORKER_TOKEN", "")
    if env_token and token == env_token:
        return True
    # DB 설정 확인 (레거시/수동 설정)
    cfg = await _get_setting(session, "bg_worker", tenant_id=tenant_id)
    if not cfg or not isinstance(cfg, dict):
        return False
    return cfg.get("worker_token", "") == token


@bg_worker_router.get("/bg-jobs/config")
async def bg_jobs_config(
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """워커 시작 시 호출 — 토큰 검증 후 R2 자격증명 반환.
    토큰 미설정 상태에서 토큰 없이 요청 시 자동 부트스트랩 (최초 1회).
    """
    import os
    import secrets

    from ._helpers import _set_setting

    _tid = _default_tenant_id()
    cfg = await _get_setting(session, "bg_worker", tenant_id=_tid)
    db_token = (cfg or {}).get("worker_token", "") if isinstance(cfg, dict) else ""

    # 로컬 워커용 토큰 미설정 + 토큰 없이 요청 → 자동 생성 후 워커에게 반환 (최초 1회)
    # env_token은 VM 워커용이므로 체크 제외
    if not db_token and not x_worker_token:
        new_token = secrets.token_hex(32)
        await _set_setting(
            session, "bg_worker", {"worker_token": new_token}, tenant_id=_tid
        )
        os.environ["BG_WORKER_TOKEN"] = new_token
        logger.info("[배경제거] 워커 토큰 자동 생성 완료")
        r2 = await _get_setting(session, "cloudflare_r2", tenant_id=_tid)
        if not r2 or not isinstance(r2, dict):
            return {"success": False, "message": "R2 설정이 저장되지 않았습니다"}
        return {
            "success": True,
            "worker_token": new_token,
            "r2": {
                "account_id": r2.get("accountId", ""),
                "access_key": r2.get("accessKey", ""),
                "secret_key": r2.get("secretKey", ""),
                "bucket": r2.get("bucketName", ""),
                "public_url": r2.get("publicUrl", ""),
            },
        }

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"success": False, "message": "Invalid worker token"}

    r2 = await _get_setting(session, "cloudflare_r2", tenant_id=_tid)
    if not r2 or not isinstance(r2, dict):
        return {"success": False, "message": "R2 설정이 저장되지 않았습니다"}

    return {
        "success": True,
        "r2": {
            "account_id": r2.get("accountId", ""),
            "access_key": r2.get("accessKey", ""),
            "secret_key": r2.get("secretKey", ""),
            "bucket": r2.get("bucketName", ""),
            "public_url": r2.get("publicUrl", ""),
        },
    }


@bg_worker_router.get("/bg-jobs/next")
async def bg_jobs_next(
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """로컬 워커 폴링 — 대기 중인 배경제거 작업 1건 반환 (없으면 null)."""
    from sqlalchemy import select as sa_select

    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.job.model import JobStatus, SambaJob

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"error": "Invalid worker token"}

    # 가장 오래된 pending 잡 1개 조회
    stmt = (
        sa_select(SambaJob)
        .where(SambaJob.job_type == "bg_remove")
        .where(SambaJob.status == JobStatus.PENDING)
        .order_by(SambaJob.created_at)
        .limit(1)
    )
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        return {"job": None}

    # running으로 상태 전환
    job.status = JobStatus.RUNNING
    from datetime import datetime, timezone

    job.started_at = datetime.now(timezone.utc)
    session.add(job)

    # 상품별 이미지 URL 조회
    payload = job.payload or {}
    product_ids: list[str] = payload.get("product_ids", [])
    scope: dict = payload.get(
        "scope", {"thumbnail": True, "additional": False, "detail": False}
    )

    products_data = []
    if product_ids:
        prod_stmt = sa_select(SambaCollectedProduct).where(
            SambaCollectedProduct.id.in_(product_ids)
        )
        prod_result = await session.execute(prod_stmt)
        prods = prod_result.scalars().all()
        for p in prods:
            products_data.append(
                {
                    "product_id": p.id,
                    "images": p.images or [],
                    "detail_images": p.detail_images or [],
                    "tags": p.tags or [],
                }
            )

    await session.commit()

    return {
        "job": {
            "job_id": job.id,
            "scope": scope,
            "products": products_data,
        }
    }


@bg_worker_router.post("/bg-jobs/{job_id}/complete")
async def bg_jobs_complete(
    job_id: str,
    request: dict[str, Any],
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """로컬 워커 완료 보고 — 각 상품 이미지 URL 업데이트 + 잡 상태 완료 처리."""
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.job.model import JobStatus, SambaJob

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"success": False, "message": "Invalid worker token"}

    stmt = sa_select(SambaJob).where(SambaJob.id == job_id)
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        return {"success": False, "message": "Job not found"}

    results: list[dict] = request.get("results", [])
    # progress 단계에서 이미 즉시 반영된 상품은 skip (idempotent)
    existing_res = dict(job.result or {})
    already_processed: set[str] = set(existing_res.get("processed_product_ids", []))
    success_count = int(existing_res.get("total_transformed", 0))
    fail_count = int(existing_res.get("total_failed", 0))

    for item in results:
        pid = item.get("product_id")
        if not pid or pid in already_processed:
            continue
        prod_stmt = sa_select(SambaCollectedProduct).where(
            SambaCollectedProduct.id == pid
        )
        prod_result = await session.execute(prod_stmt)
        product = prod_result.scalar_one_or_none()
        if not product:
            fail_count += 1
            continue

        if item.get("success"):
            new_images = item.get("new_images")
            new_detail = item.get("new_detail_images")
            new_tags = list(
                set((product.tags or []) + ["__ai_image__", "__img_edited__"])
            )

            if new_images is not None:
                product.images = new_images
            if new_detail is not None:
                product.detail_images = new_detail
            product.tags = new_tags
            session.add(product)
            success_count += 1
        else:
            fail_count += 1

    # 취소된 잡은 상태 유지(cancelled), 결과만 누적
    is_cancelled_report = (
        bool(request.get("cancelled")) or job.status == JobStatus.CANCELLED
    )
    if not is_cancelled_report:
        job.status = JobStatus.COMPLETED
    job.completed_at = datetime.now(timezone.utc)
    job.current = success_count
    job.result = {"total_transformed": success_count, "total_failed": fail_count}
    session.add(job)
    await session.commit()

    logger.info(
        f"[배경제거] 완료: job_id={job_id}, 성공={success_count}, 실패={fail_count}"
        f"{', 취소됨' if is_cancelled_report else ''}"
    )
    return {
        "success": True,
        "total_transformed": success_count,
        "total_failed": fail_count,
    }


@bg_worker_router.patch("/bg-jobs/{job_id}/progress")
async def bg_jobs_progress(
    job_id: str,
    request: dict[str, Any] | None = None,
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """워커 진행률 보고.

    body 없음 → 상품 1건 완료(current +1)
    body에 image_current/image_total 있음 → 사진 단위 진행률만 갱신
      (current는 증가시키지 않음 — 상품 완료는 별도 호출 또는 complete에서 처리)
    body.product_result 있음 → 상품 1건 완료를 즉시 DB 반영
      (이미지 URL/태그 즉시 커밋 + total_transformed/total_failed 누적)
    """
    from sqlalchemy import select as sa_select

    from backend.domain.samba.collector.model import SambaCollectedProduct
    from backend.domain.samba.job.model import SambaJob

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"success": False, "message": "Invalid worker token"}

    stmt = sa_select(SambaJob).where(SambaJob.id == job_id)
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        return {"success": False, "message": "Job not found"}

    body = request or {}
    img_cur = body.get("image_current")
    img_tot = body.get("image_total")
    cur_pid = body.get("current_product_id")
    bump_product = bool(body.get("bump_product", img_cur is None))
    product_result = body.get("product_result")

    if bump_product:
        job.current = min(job.current + 1, job.total)

    # 사진 단위 진행률은 result JSON에 임시 저장(스키마 변경 없이 status에서 노출)
    res = dict(job.result or {})
    if img_cur is not None:
        res["image_current"] = int(img_cur)
    if img_tot is not None:
        res["image_total"] = int(img_tot)
    if cur_pid is not None:
        res["current_product_id"] = str(cur_pid)

    # 상품 단위 즉시 DB 반영 — 잡 종료 전이라도 새로고침 시 반영되도록
    if isinstance(product_result, dict):
        pid = product_result.get("product_id")
        if pid:
            prod_stmt = sa_select(SambaCollectedProduct).where(
                SambaCollectedProduct.id == pid
            )
            prod_result = await session.execute(prod_stmt)
            product = prod_result.scalar_one_or_none()
            if product and product_result.get("success"):
                new_images = product_result.get("new_images")
                new_detail = product_result.get("new_detail_images")
                if new_images is not None:
                    product.images = new_images
                if new_detail is not None:
                    product.detail_images = new_detail
                product.tags = list(
                    set((product.tags or []) + ["__ai_image__", "__img_edited__"])
                )
                session.add(product)
                res["total_transformed"] = int(res.get("total_transformed", 0)) + 1
            else:
                res["total_failed"] = int(res.get("total_failed", 0)) + 1
            res["processed_product_ids"] = list(
                set(res.get("processed_product_ids", []) + [pid])
            )

    job.result = res

    session.add(job)
    await session.commit()
    return {"success": True, "current": job.current, "total": job.total}


@router.get("/bg-jobs/{job_id}/status")
async def bg_jobs_status(
    job_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """프론트엔드 폴링용 — 배경제거 잡 상태 조회."""
    from sqlalchemy import select as sa_select

    from backend.domain.samba.job.model import SambaJob

    stmt = sa_select(SambaJob).where(SambaJob.id == job_id)
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        return {"status": "not_found"}

    res = job.result or {}
    return {
        "status": job.status,
        "total": job.total,
        "current": job.current,
        "total_transformed": res.get("total_transformed", 0),
        "total_failed": res.get("total_failed", 0),
        "image_current": res.get("image_current"),
        "image_total": res.get("image_total"),
        "current_product_id": res.get("current_product_id"),
    }


# ═══════════════════════════════════════════════
# 배경제거 잡 큐 — 활성 잡 목록 + 취소
# ═══════════════════════════════════════════════


@router.get("/bg-jobs/active")
async def bg_jobs_active(
    session: AsyncSession = Depends(get_read_session_dependency),
) -> dict[str, Any]:
    """현재 진행 중(pending/running)인 배경제거 잡 목록 + 워커 헬스 — 모달 표시용."""
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from backend.domain.samba.job.model import JobStatus, SambaJob

    stmt = (
        sa_select(SambaJob)
        .where(SambaJob.job_type == "bg_remove")
        .where(SambaJob.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
        .order_by(SambaJob.created_at)
    )
    result = await session.execute(stmt)
    jobs = result.scalars().all()

    # 워커 헬스 — 30초 안에 heartbeat 있으면 alive
    last_seen_str = await _get_setting(session, "bg_worker_last_seen") or ""
    worker_alive = False
    if last_seen_str:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen_str)
            now = datetime.now(timezone.utc)
            worker_alive = (now - last_seen_dt).total_seconds() <= 30
        except (ValueError, TypeError):
            worker_alive = False

    return {
        "jobs": [
            {
                "job_id": j.id,
                "status": j.status,
                "total": j.total,
                "current": j.current,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
            }
            for j in jobs
        ],
        "worker_alive": worker_alive,
        "worker_last_seen": last_seen_str or None,
    }


@router.post("/bg-jobs/{job_id}/cancel")
async def bg_jobs_cancel(
    job_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """배경제거 잡 취소 — pending이면 즉시, running이면 워커가 다음 상품 진입 전 중단."""
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from backend.domain.samba.job.model import JobStatus, SambaJob

    stmt = sa_select(SambaJob).where(SambaJob.id == job_id)
    result = await session.execute(stmt)
    job = result.scalar_one_or_none()
    if not job:
        return {"success": False, "message": "잡을 찾을 수 없습니다"}

    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return {
            "success": False,
            "message": f"이미 종료된 잡입니다 (status={job.status})",
        }

    job.status = JobStatus.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    session.add(job)
    await session.commit()

    logger.info(f"[배경제거] 잡 취소: job_id={job_id}")
    return {"success": True, "job_id": job_id, "status": "cancelled"}


# ═══════════════════════════════════════════════
# 워커 부팅 시 stuck running 잡 정리 + heartbeat
# ═══════════════════════════════════════════════


@bg_worker_router.post("/bg-jobs/worker-reset-running")
async def bg_jobs_worker_reset_running(
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """워커 부팅 시 호출 — running 상태로 잔존한 bg_remove 잡을 cancelled로 정리.

    워커가 잡 픽업 후 비정상 종료(OOM/프로세스 강제 kill)되면 status=running으로 영구 잔존.
    워커 1대 운영 환경에서는 새 워커 부팅 시 이전 stuck 잡은 무조건 죽은 것으로 간주 가능.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select

    from backend.domain.samba.job.model import JobStatus, SambaJob

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"success": False, "message": "Invalid worker token"}

    stmt = (
        sa_select(SambaJob)
        .where(SambaJob.job_type == "bg_remove")
        .where(SambaJob.status == JobStatus.RUNNING)
    )
    result = await session.execute(stmt)
    stuck_jobs = result.scalars().all()

    now = datetime.now(timezone.utc)
    reset_ids: list[str] = []
    for j in stuck_jobs:
        j.status = JobStatus.CANCELLED
        j.completed_at = now
        j.error = "worker restart — stuck running 자동 정리"
        session.add(j)
        reset_ids.append(j.id)

    if reset_ids:
        await session.commit()
        logger.info(
            f"[배경제거] 워커 부팅 stuck 잡 정리: {len(reset_ids)}건 — {reset_ids}"
        )

    return {"success": True, "reset_count": len(reset_ids), "reset_ids": reset_ids}


@bg_worker_router.patch("/bg-jobs/heartbeat")
async def bg_jobs_heartbeat(
    x_worker_token: str = Header(default=""),
    session: AsyncSession = Depends(get_write_session_dependency),
) -> dict[str, Any]:
    """워커 헬스체크 — 매 폴링마다 호출되어 last_seen 갱신."""
    from datetime import datetime, timezone

    if not await _verify_worker_token(
        x_worker_token, session, tenant_id=_default_tenant_id()
    ):
        return {"success": False, "message": "Invalid worker token"}

    now = datetime.now(timezone.utc).isoformat()
    await _set_setting(session, "bg_worker_last_seen", now)
    return {"success": True, "last_seen": now}


# ═══════════════════════════════════════════════
# 워커 자동 설치 — install_bg_worker.bat 다운로드
# ═══════════════════════════════════════════════


@bg_worker_router.get("/bg-jobs/installer")
async def bg_jobs_installer() -> Response:
    """배경제거 워커 설치 패키지(ZIP) 다운로드.

    프론트의 '배경제거' 버튼 클릭 시 워커가 죽어있으면 이 엔드포인트로 안내.
    ZIP 안에:
      - install.bat          : Python 자동설치 + 표준 경로 복사 + 작업 스케줄러 등록 + 즉시 실행
      - local_bg_worker.py   : 워커 본체
      - bg_worker.env        : 사용자별 백엔드 URL이 자동 주입된 설정
    사용자는 ZIP 다운로드 → 압축 풀기 → install.bat 더블클릭 1회로 영구 구동.
    """
    import io
    import os
    import zipfile
    from pathlib import Path

    scripts_dir = Path(__file__).resolve().parents[6] / "scripts"
    worker_py = scripts_dir / "local_bg_worker.py"
    install_bat = scripts_dir / "install_bg_worker.bat"
    watchdog_ps1 = scripts_dir / "bg_worker_watchdog_template.ps1"
    watchdog_vbs = scripts_dir / "bg_worker_watchdog_template.vbs"

    if not worker_py.exists() or not install_bat.exists():
        return Response(
            content="installer files not found",
            status_code=404,
            media_type="text/plain",
        )

    api_url = os.environ.get(
        "PUBLIC_API_URL",
        os.environ.get("SAMBA_API_URL", "https://api.samba-wave.co.kr"),
    )
    env_text = (
        "# Samba Wave Local BG Worker Config\n"
        "# 자동 생성된 설정 — 백엔드 URL은 다운로드 시점에 주입됨\n"
        f"SAMBA_API_URL={api_url}\n"
        "WORKER_TOKEN=\n"
        "POLL_INTERVAL=5\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("install.bat", install_bat.read_text(encoding="utf-8"))
        z.writestr("local_bg_worker.py", worker_py.read_text(encoding="utf-8"))
        z.writestr("bg_worker.env", env_text)
        # 워치독 템플릿 — install.bat이 표준 경로 치환 후 사용
        if watchdog_ps1.exists():
            z.writestr(
                "bg_worker_watchdog.ps1",
                watchdog_ps1.read_text(encoding="utf-8"),
            )
        if watchdog_vbs.exists():
            # VBS는 ASCII만 (wscript 한글 주석 파싱 실패 이슈)
            z.writestr(
                "bg_worker_watchdog.vbs",
                watchdog_vbs.read_text(encoding="ascii", errors="ignore"),
            )
        z.writestr(
            "README.txt",
            "Samba Wave 배경제거 워커 설치\n"
            "\n"
            "1. 이 ZIP을 원하는 폴더에 압축 해제하세요.\n"
            "2. install.bat 을 더블클릭하세요.\n"
            "3. 끝. 워커는 자동 등록되어 PC 재부팅 후에도 자동 실행됩니다.\n"
            "\n"
            "필요사항: Python 3.10 이상 (없으면 install.bat 이 자동 설치 시도)\n"
            "설치 위치: %LOCALAPPDATA%\\SambaWave\\bg-worker\\\n"
            "\n"
            "제거: 작업스케줄러에서 SambaWaveBgWorkerWatchdog 삭제\n",
        )
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="samba-bg-worker.zip"'},
    )
