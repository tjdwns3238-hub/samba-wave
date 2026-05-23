"""스마트스토어 auth/설정 관련 엔드포인트."""

from __future__ import annotations

import base64
import time
from typing import Any, Optional

import bcrypt
import httpx
from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.utils.logger import logger

from ._helpers import _get_setting, _get_ss_client

router = APIRouter(tags=["samba-proxy"])


@router.get("/smartstore/search-brand")
async def smartstore_search_brand(
    name: str = Query(...),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> list[dict[str, Any]]:
    """스마트스토어 브랜드 검색."""
    client = await _get_ss_client(session)
    if not client:
        return []
    result = await client._call_api("GET", "/v1/product-brands", params={"name": name})
    return result if isinstance(result, list) else []


@router.get("/smartstore/search-manufacturer")
async def smartstore_search_manufacturer(
    name: str = Query(...),
    session: AsyncSession = Depends(get_read_session_dependency),
) -> list[dict[str, Any]]:
    """스마트스토어 제조사 검색."""
    client = await _get_ss_client(session)
    if not client:
        return []
    result = await client._call_api(
        "GET", "/v1/product-manufacturers", params={"name": name}
    )
    return result if isinstance(result, list) else []


@router.post("/smartstore/auth-test")
async def smartstore_auth_test(
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
) -> dict[str, Any]:
    """스마트스토어 Commerce API 인증 테스트 — OAuth2 토큰 발급 시도."""
    creds = await _get_setting(session, "store_smartstore", tenant_id=tenant_id)
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "스마트스토어 설정이 저장되지 않았습니다."}

    client_id = creds.get("clientId", "")
    client_secret = creds.get("clientSecret", "")
    if not client_id or not client_secret:
        return {
            "success": False,
            "message": "Client ID 또는 Client Secret이 비어있습니다.",
        }

    try:
        # bcrypt 서명 생성 (네이버 Commerce API 인증 방식)
        # 네이버 Commerce API clientSecret 은 29자 bcrypt salt 형식.
        # $2y$ prefix 는 Python bcrypt 가 지원하지 않으므로 $2b$ 로 정규화.
        # $2a$ / $2b$ 는 그대로 사용해야 서명이 일치한다.
        salt = client_secret
        if salt.startswith("$2y$"):
            salt = "$2b$" + salt[4:]
        timestamp = int(time.time() * 1000)
        password = f"{client_id}_{timestamp}"
        try:
            hashed = bcrypt.hashpw(
                password.encode("utf-8"),
                salt.encode("utf-8"),
            )
        except ValueError as ve:
            return {
                "success": False,
                "message": (
                    f"clientSecret 형식 오류 ({ve}). 네이버 커머스 API 센터에서 "
                    "발급한 29자 bcrypt salt($2a$/$2b$ prefix)를 그대로 저장하세요."
                ),
            }
        client_secret_sign = base64.standard_b64encode(hashed).decode("utf-8")

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.commerce.naver.com/external/v1/oauth2/token",
                data={
                    "client_id": client_id,
                    "timestamp": timestamp,
                    "client_secret_sign": client_secret_sign,
                    "grant_type": "client_credentials",
                    "type": "SELF",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("access_token", "")
                expires = data.get("expires_in", 0)
                return {
                    "success": True,
                    "message": f"인증 성공 (토큰 유효시간: {expires // 3600}시간)",
                    "token_preview": f"{token[:12]}..." if len(token) > 12 else token,
                }
            else:
                err = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                return {
                    "success": False,
                    "message": err.get("message")
                    or err.get("error_description")
                    or f"HTTP {resp.status_code}",
                }
    except Exception as exc:
        logger.error(f"[스마트스토어] 인증 테스트 실패: {exc}")
        return {"success": False, "message": f"API 호출 실패: {exc}"}
