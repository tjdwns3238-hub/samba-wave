"""무신사 쿠키 JWT(mss_mac) 디코더 — 외부 호출 없이 hashId/등급 추출.

mss_mac JWT payload 예:
  {
    "sub": "<32hex hashId>",
    "hashedUid": "<64hex>",
    "groupLevel": "9",
    "gender": "M",
    "birthYear": "1982",
    "registerDate": "2023-06-12",
    "orderCount": "1489",
    ...
  }

쿠키 소유자 식별 단일 기준: sub(hashId). 무신사 계정 고유.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

_MSS_MAC_RE = re.compile(r"(?:^|;\s*)mss_mac=([^;]+)")


def _b64url_decode(seg: str) -> bytes:
    seg = seg + "=" * ((4 - len(seg) % 4) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_mss_mac(cookie: str) -> Optional[dict[str, Any]]:
    """쿠키 문자열에서 mss_mac JWT payload 디코딩. 실패 시 None."""
    if not cookie:
        return None
    m = _MSS_MAC_RE.search(cookie)
    if not m:
        return None
    token = m.group(1).strip()
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        payload = json.loads(_b64url_decode(parts[1]))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def musinsa_hash_id(cookie: str) -> Optional[str]:
    """쿠키 주인 hashId(sub) 추출. 무신사 계정 고유 식별자."""
    p = decode_mss_mac(cookie)
    if not p:
        return None
    sub = p.get("sub")
    return sub if isinstance(sub, str) and sub else None


def musinsa_account_brief(cookie: str) -> Optional[dict[str, Any]]:
    """UI 표시용 요약. 등급/성별/가입일 등."""
    p = decode_mss_mac(cookie)
    if not p:
        return None
    level_raw = p.get("groupLevel")
    try:
        level = int(level_raw) if level_raw is not None else None
    except (TypeError, ValueError):
        level = None
    order_count_raw = p.get("orderCount")
    try:
        order_count = int(order_count_raw) if order_count_raw is not None else None
    except (TypeError, ValueError):
        order_count = None
    return {
        "hash_id": p.get("sub"),
        "level": level,
        "gender": p.get("gender"),
        "birth_year": p.get("birthYear"),
        "register_date": p.get("registerDate"),
        "order_count": order_count,
    }
