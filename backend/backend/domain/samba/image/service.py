"""이미지 변환 서비스 — rembg(배경제거) + FLUX(착용컷/연출컷) + Cloudflare R2/로컬 저장."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)

# 로컬 저장 경로
LOCAL_IMAGE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "static" / "images"
)
LOCAL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

# 프리셋 이미지 로컬 경로
PRESET_IMAGE_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "static" / "model_presets"
)

# rembg U2-Net 세션 싱글턴 캐시 (최초 1회만 로드, 이후 재사용)
_rembg_session_cache: dict[str, Any] = {}


def _get_rembg_session() -> Any:
    """rembg U2-Net 세션 반환 (프로세스 생애주기 동안 1회만 초기화)."""
    if "session" not in _rembg_session_cache:
        from rembg import new_session

        # isnet-general-use: 패션/일반 객체 분리에 silueta보다 깔끔 (잔상 감소)
        _rembg_session_cache["session"] = new_session("isnet-general-use")
    return _rembg_session_cache["session"]


# ──────────────────────────────────────────────
# 모델 프리셋 (12개) — image: 참조 이미지 파일명
# ──────────────────────────────────────────────
MODEL_PRESETS: dict[str, dict[str, str]] = {
    # 성인 여성 — 파리 하이패션 런웨이 모델
    "female_v1": {
        "label": "여성 — 쿨 스트레이트",
        "desc": "22세 백인 서양인 여성 패션모델, 쇄골 아래 길이 스트레이트 브론드 헤어, 날카로운 턱선, 높은 광대뼈, 170cm, 긴 목선, 무표정에 가까운 쿨한 눈빛, 한쪽 어깨를 살짝 앞으로 내민 런웨이 포즈, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "female_v1.png",
    },
    "female_v2": {
        "label": "여성 — 샤프 보브컷",
        "desc": "23세 백인 서양인 여성 패션모델, 턱선 애쉬브라운 보브컷, 샤프한 이목구비, 각진 어깨라인, 172cm, 시선을 약간 내린 언뉘 표정, 체중을 한쪽 다리에 실은 콘트라포스토 자세, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "female_v2.png",
    },
    "female_v3": {
        "label": "여성 — 에포트리스 웨이브",
        "desc": "21세 백인 서양인 여성 패션모델, 센터파팅 느슨한 다크브라운 웨이브, 얇은 눈썹, 길고 가는 팔다리, 174cm, 입술을 살짝 벌린 무심한 표정, 손끝을 허벅지에 가볍게 댄 이지 포즈, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "female_v3.png",
    },
    # 성인 남성 — 파리 하이패션 런웨이 모델
    "male_v1": {
        "label": "남성 — 클린 크롭",
        "desc": "24세 백인 서양인 남성 패션모델, 짧은 텍스처드 브론드 크롭, 날카로운 턱선, 좁은 얼굴형, 183cm, 무표정의 날카로운 눈빛, 양손을 자연스럽게 늘어뜨린 런웨이 워킹 포즈, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "male_v1.png",
    },
    "male_v2": {
        "label": "남성 — 슬릭백 스트롱",
        "desc": "25세 백인 서양인 남성 패션모델, 다크브라운 슬릭백 헤어, 강한 골격, 넓은 어깨에 긴 팔다리, 186cm, 턱을 살짝 든 도도한 시선, 한 발 앞으로 내딛는 스트라이드 포즈, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "male_v2.png",
    },
    "male_v3": {
        "label": "남성 — 앤드로지너스",
        "desc": "23세 백인 서양인 남성 패션모델, 센터파팅 내추럴 라이트브라운 헤어, 섬세한 이목구비, 가는 체형, 182cm, 살짝 고개 돌린 사이드 시선, 안드로지너스한 분위기, 손을 가볍게 주머니에 걸친 포즈, 파리 꾸레쥬 컬렉션 런웨이 모델",
        "image": "male_v3.png",
    },
    # 키즈 여아
    "kids_girl_v1": {
        "label": "키즈여아 — 긴머리 차분",
        "desc": "8세 백인 서양인 여아, 어깨 아래 긴 생머리, 130cm, 차분하게 서있는 포즈, 양손 자연스럽게 내림",
        "image": "kids_girl_v1.png",
    },
    "kids_girl_v2": {
        "label": "키즈여아 — 단발 활발",
        "desc": "8세 백인 서양인 여아, 턱선 단발머리, 128cm, 양팔 벌린 활발한 포즈, 밝은 표정",
        "image": "kids_girl_v2.png",
    },
    "kids_girl_v3": {
        "label": "키즈여아 — 양갈래 귀여움",
        "desc": "8세 백인 서양인 여아, 양갈래 묶은머리, 130cm, 귀여운 미소, 자연스러운 포즈",
        "image": "kids_girl_v3.png",
    },
    # 키즈 남아
    "kids_boy_v1": {
        "label": "키즈남아 — 밝은 정면",
        "desc": "8세 백인 서양인 남아, 짧은 머리, 130cm, 밝은 미소, 양손 주머니에 넣고 정면 포즈",
        "image": "kids_boy_v1.png",
    },
    "kids_boy_v2": {
        "label": "키즈남아 — 장난꾸러기",
        "desc": "8세 백인 서양인 남아, 짧은 머리, 128cm, 한쪽 다리 들고 점프하는 역동적 포즈, 장난꾸러기 표정",
        "image": "kids_boy_v2.png",
    },
    "kids_boy_v3": {
        "label": "키즈남아 — 차분한",
        "desc": "8세 백인 서양인 남아, 약간 긴 앞머리, 130cm, 양손 내리고 차분하게 서있는 포즈, 반바지 착용",
        "image": "kids_boy_v3.png",
    },
}


# ──────────────────────────────────────────────
# Gemini 이미지 변환 한국어 프롬프트
# ──────────────────────────────────────────────
def _get_category_prompt(category: str, mode: str, model_desc: str) -> str:
    """카테고리 + 모드 + 모델 프리셋으로 프롬프트 생성."""
    cat_lower = (category or "").lower()

    # 카테고리 감지
    if any(k in cat_lower for k in ["등산화", "트레킹"]):
        cat_type = "hiking_shoes"
    elif any(
        k in cat_lower for k in ["런닝화", "러닝화", "운동화", "스니커즈", "스포츠화"]
    ):
        cat_type = "sneakers"
    elif any(k in cat_lower for k in ["구두", "로퍼", "옥스포드", "더비"]):
        cat_type = "dress_shoes"
    elif any(k in cat_lower for k in ["샌들", "슬리퍼"]):
        cat_type = "sandals"
    elif any(k in cat_lower for k in ["부츠"]):
        cat_type = "boots"
    elif any(k in cat_lower for k in ["신발"]):
        cat_type = "shoes"
    elif any(
        k in cat_lower
        for k in ["아우터", "자켓", "재킷", "코트", "점퍼", "패딩", "윈드"]
    ):
        cat_type = "outer"
    elif any(
        k in cat_lower
        for k in ["상의", "셔츠", "니트", "티셔츠", "블라우스", "맨투맨", "후드"]
    ):
        cat_type = "top"
    elif any(
        k in cat_lower for k in ["하의", "바지", "팬츠", "스커트", "치마", "레깅스"]
    ):
        cat_type = "bottom"
    elif any(k in cat_lower for k in ["가방", "백팩", "토트", "크로스백", "숄더백"]):
        cat_type = "bag"
    elif any(k in cat_lower for k in ["모자", "캡", "비니", "버킷햇"]):
        cat_type = "hat"
    elif any(k in cat_lower for k in ["뷰티", "화장품", "스킨케어", "향수"]):
        cat_type = "beauty"
    else:
        cat_type = "general"

    if mode == "background":
        return "이 상품 사진에서 배경을 제거하고, 순수 흰색 배경 위에 상품만 깔끔하게 배치해주세요. 상품의 색상, 디자인, 디테일을 100% 정확하게 유지해주세요. 그림자 없이 깨끗하게."

    if mode == "model_to_product":
        m2p_map = {
            "hiking_shoes": "이 사진에서 사람을 완전히 제거하고, 신발만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "sneakers": "이 사진에서 사람을 완전히 제거하고, 운동화만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "dress_shoes": "이 사진에서 사람을 완전히 제거하고, 구두만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "sandals": "이 사진에서 사람을 완전히 제거하고, 샌들만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "boots": "이 사진에서 사람을 완전히 제거하고, 부츠만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "shoes": "이 사진에서 사람을 완전히 제거하고, 신발만 순수 흰색 배경 위에 45도 각도로 놓인 상품 사진으로 변환해주세요.",
            "outer": "이 사진에서 사람을 완전히 제거하고, 아우터만 보이지 않는 마네킹에 걸린 것처럼 순수 흰색 배경 위에 정면으로 보여주세요. 고스트 마네킹 스타일.",
            "top": "이 사진에서 사람을 완전히 제거하고, 상의만 보이지 않는 마네킹에 걸린 것처럼 순수 흰색 배경 위에 정면으로 보여주세요. 고스트 마네킹 스타일.",
            "bottom": "이 사진에서 사람을 완전히 제거하고, 하의만 순수 흰색 배경 위에 평평하게 펼쳐놓은 플랫레이 스타일로 보여주세요.",
            "bag": "이 사진에서 사람을 완전히 제거하고, 가방만 순수 흰색 배경 위에 정면으로 놓인 상품 사진으로 변환해주세요.",
            "hat": "이 사진에서 사람을 완전히 제거하고, 모자만 순수 흰색 배경 위에 놓인 상품 사진으로 변환해주세요.",
            "beauty": "이 사진에서 사람을 완전히 제거하고, 제품만 순수 흰색 배경 위에 정면으로 놓인 상품 사진으로 변환해주세요.",
            "general": "이 사진에서 사람을 완전히 제거하고, 상품만 순수 흰색 배경 위에 놓인 깔끔한 상품 사진으로 변환해주세요.",
        }
        prompt = m2p_map.get(cat_type, m2p_map["general"])
        return (
            prompt
            + " 상품의 색상, 디자인, 로고, 패턴, 소재 질감을 100% 정확하게 유지해주세요."
            " 쇼핑몰 상품 상세페이지에 사용할 전문 상품 사진 스타일."
            " 절대 금지: 사람의 신체 일부(손, 발, 얼굴, 피부)가 남아있으면 안 됩니다."
            " 절대 금지: 원본에 없는 로고, 텍스트, 브랜드 마크를 추가하지 마세요."
        )

    if mode == "scene":
        scene_map = {
            "hiking_shoes": "산길 옆 나무 벤치 위에 자연스럽게 놓인 모습, 아웃도어 감성",
            "sneakers": "카페 테이블 위에 깔끔하게 놓인 플랫레이, 미니멀한 라이프스타일",
            "dress_shoes": "대리석 바닥 위에 우아하게 놓인 모습, 고급스러운 분위기",
            "sandals": "해변가 모래 위, 여름 감성, 자연광",
            "boots": "가을 낙엽 위에 놓인 모습, 따뜻한 톤",
            "shoes": "깔끔한 나무 바닥 위에 놓인 모습",
            "outer": "옷걸이에 걸린 모습, 깔끔한 옷장 배경",
            "top": "깔끔하게 접혀서 나무 선반 위에 놓인 플랫레이",
            "bottom": "깔끔하게 접혀서 놓인 플랫레이, 미니멀 배경",
            "bag": "카페 테이블 위에 자연스럽게 놓인 모습, 소품과 함께",
            "hat": "나무 테이블 위에 놓인 모습, 자연광",
            "beauty": "대리석 위에 놓인 모습, 꽃잎 장식, 고급스러운 뷰티 화보",
            "general": "깔끔한 배경에 자연스럽게 배치된 제품 사진",
        }
        scene = scene_map.get(cat_type, scene_map["general"])
        return f"이 상품 사진을 참고해서, {scene} 연출컷을 만들어주세요. 상품의 색상, 디자인, 로고, 디테일을 100% 정확하게 유지해주세요. 전문 매거진 에디토리얼 스타일."

    if mode == "video":
        video_map = {
            "hiking_shoes": f"이 등산화 사진을 참고해서, {model_desc}이(가) 이 신발을 신고 바위 위에 한 발을 올려놓은 채 먼 산을 응시하는 전신 사진을 생성해주세요. 테크니컬 아우터와 카고팬츠, 안개 낀 산속 새벽빛, 무표정하고 강인한 눈빛, 하이패션 아웃도어 에디토리얼.",
            "sneakers": f"이 운동화 사진을 참고해서, {model_desc}이(가) 이 신발을 신고 콘크리트 도심 골목에서 한 발을 앞으로 내딛는 런웨이 워킹 전신 사진을 생성해주세요. 오버사이즈 코트에 와이드팬츠, 무심한 시선으로 카메라 옆을 응시, 스트릿 하이패션 무드.",
            "dress_shoes": f"이 구두 사진을 참고해서, {model_desc}이(가) 이 구두를 신고 대리석 계단에 서 있는 전신 사진을 생성해주세요. 테일러드 수트, 한 손을 주머니에 넣고 턱을 살짝 든 자세, 쿨한 무표정, 클래식 하이패션 에디토리얼.",
            "sandals": f"이 샌들 사진을 참고해서, {model_desc}이(가) 이 샌들을 신고 백사장 위를 걸어가는 전신 사진을 생성해주세요. 리넨 셔츠와 와이드팬츠, 바람에 옷이 자연스럽게 날리는 순간포착, 시선은 수평선 너머, 리조트 에디토리얼.",
            "boots": f"이 부츠 사진을 참고해서, {model_desc}이(가) 이 부츠를 신고 젖은 도시 거리에 서 있는 전신 사진을 생성해주세요. 롱코트에 턱을 숨기고 한쪽 무릎을 살짝 구부린 포즈, 가로등 불빛 반사, 차가운 무표정, 시네마틱 에디토리얼.",
            "shoes": f"이 신발 사진을 참고해서, {model_desc}이(가) 이 신발을 신고 미니멀한 콘크리트 공간에서 걸어가는 전신 사진을 생성해주세요. 모노톤 스타일링, 자신감 있는 스트라이드, 쿨한 시선, 하이패션 에디토리얼.",
            "outer": f"이 아우터 사진을 참고해서, {model_desc}이(가) 이 아우터를 입고 도심 빌딩 사이 빈 거리를 걸어오는 전신 사진을 생성해주세요. 한 손으로 옷깃을 잡고 바람에 맞서는 포즈, 시선은 카메라 너머 먼 곳, 런웨이 에디토리얼.",
            "top": f"이 상의 사진을 참고해서, {model_desc}이(가) 이 옷을 입고 콘크리트 벽에 어깨를 기대 선 전신 사진을 생성해주세요. 한쪽 팔을 자연스럽게 늘어뜨리고 살짝 고개를 기울인 포즈, 무심한 눈빛, 미니멀 하이패션 무드.",
            "bottom": f"이 하의 사진을 참고해서, {model_desc}이(가) 이 옷을 입고 빈 런웨이 같은 긴 복도를 걸어오는 전신 사진을 생성해주세요. 미니멀한 상의 매치, 자신감 있는 스트라이드, 정면을 똑바로 응시, 런웨이 에디토리얼.",
            "bag": f"이 가방 사진을 참고해서, {model_desc}이(가) 이 가방을 한 손에 가볍게 들고 계단을 내려오는 전신 사진을 생성해주세요. 모노톤 스타일링, 시선을 살짝 내린 언뉘 표정, 하이패션 스트릿 에디토리얼.",
            "hat": f"이 모자 사진을 참고해서, {model_desc}이(가) 이 모자를 쓰고 역광 속 도시 옥상에서 포즈를 취하는 전신 사진을 생성해주세요. 미니멀 코디, 턱을 살짝 든 자세로 하늘을 응시, 시네마틱 하이패션 에디토리얼.",
            "beauty": f"이 뷰티 제품 사진을 참고해서, {model_desc}이(가) 이 제품을 턱 아래로 가볍게 든 클로즈업 사진을 생성해주세요. 글로시한 피부, 입술을 살짝 벌린 무심한 표정, 소프트 사이드 라이팅, 하이엔드 뷰티 에디토리얼.",
            "general": f"이 상품 사진을 참고해서, {model_desc}이(가) 이 상품을 사용하고 있는 전신 사진을 생성해주세요. 미니멀한 콘크리트 배경, 런웨이 모델의 자신감 있는 자세, 무표정의 쿨한 눈빛, 하이패션 에디토리얼.",
        }
        prompt = video_map.get(cat_type, video_map["general"])
        return (
            prompt
            + " 상품의 색상, 디자인, 로고, 디테일을 100% 정확하게 유지해주세요. 파리 하이패션 에디토리얼 스타일, AI 느낌이 나지 않는 실제 화보처럼, 9:16 세로 구도."
        )

    # mode == "model" — 하이패션 착용컷
    model_prompt_map = {
        "hiking_shoes": f"이 등산화 사진을 참고해서, {model_desc}이(가) 이 신발을 착용한 발 클로즈업 사진을 생성해주세요. 무릎 아래만 보이는 구도, 테크니컬 카고팬츠에 기능성 양말, 한 발을 바위에 올린 역동적 각도, 콘크리트 그레이 배경.",
        "sneakers": f"이 운동화 사진을 참고해서, {model_desc}이(가) 이 신발을 착용한 발 클로즈업 사진을 생성해주세요. 무릎 아래만 보이는 구도, 와이드 팬츠에 노쇼 양말, 한 발을 살짝 들어 올린 워킹 순간포착, 깨끗한 콘크리트 바닥.",
        "dress_shoes": f"이 구두 사진을 참고해서, {model_desc}이(가) 이 신발을 착용한 발 클로즈업 사진을 생성해주세요. 무릎 아래만 보이는 구도, 테일러드 슬랙스, 다리를 교차한 우아한 자세, 대리석 바닥 배경.",
        "sandals": f"이 샌들 사진을 참고해서, {model_desc}이(가) 이 신발을 착용한 발 클로즈업 사진을 생성해주세요. 무릎 아래만 보이는 구도, 맨발에 앵클릿, 한 발을 앞으로 내민 자세, 밝은 자연광 배경.",
        "boots": f"이 부츠 사진을 참고해서, {model_desc}이(가) 이 부츠를 착용한 사진을 생성해주세요. 무릎 아래만 보이는 구도, 슬림 팬츠를 부츠 안에 넣은 스타일링, 체중을 한쪽에 실은 자세, 젖은 아스팔트 배경.",
        "shoes": f"이 신발 사진을 참고해서, {model_desc}이(가) 이 신발을 착용한 발 클로즈업 사진을 생성해주세요. 무릎 아래만 보이는 구도, 크롭 팬츠에 노쇼 양말, 워킹 순간포착 각도, 미니멀 그레이 배경.",
        "outer": f"이 아우터 사진을 참고해서, {model_desc}이(가) 이 아우터를 입고 있는 상반신 사진을 생성해주세요. 안에 심플한 블랙 터틀넥, 한 손으로 옷깃을 잡고 살짝 몸을 비튼 포즈, 쿨한 무표정, 라이트그레이 스튜디오 배경.",
        "top": f"이 상의 사진을 참고해서, {model_desc}이(가) 이 옷을 입고 있는 상반신 사진을 생성해주세요. 한쪽 팔꿈치를 가볍게 구부리고 시선을 카메라 옆으로 던진 포즈, 무심한 언뉘 표정, 라이트그레이 스튜디오 배경.",
        "bottom": f"이 하의 사진을 참고해서, {model_desc}이(가) 이 옷을 입고 있는 전신 사진을 생성해주세요. 미니멀 블랙 탑 매치, 한 발 앞으로 내딛는 런웨이 워킹 포즈, 정면 응시, 라이트그레이 스튜디오 배경.",
        "bag": f"이 가방 사진을 참고해서, {model_desc}이(가) 이 가방을 한 손에 가볍게 들고 있는 상반신 사진을 생성해주세요. 모노톤 의상, 턱을 살짝 든 자세로 시선을 내린 포즈, 라이트그레이 스튜디오 배경.",
        "hat": f"이 모자 사진을 참고해서, {model_desc}이(가) 이 모자를 쓰고 있는 상반신 사진을 생성해주세요. 모노톤 의상, 살짝 고개를 돌려 사이드 프로필이 보이는 포즈, 쿨한 눈빛, 라이트그레이 스튜디오 배경.",
        "beauty": f"이 뷰티 제품 사진을 참고해서, {model_desc}이(가) 이 제품을 턱선 옆에 가볍게 든 클로즈업 사진을 생성해주세요. 글로시한 피부, 입술을 살짝 벌린 무심한 표정, 소프트 사이드 라이팅, 밝은 배경.",
        "general": f"이 상품 사진을 참고해서, {model_desc}이(가) 이 상품을 사용/착용하고 있는 사진을 생성해주세요. 런웨이 모델의 자신감 있는 자세, 쿨한 무표정, 라이트그레이 스튜디오 배경.",
    }
    prompt = model_prompt_map.get(cat_type, model_prompt_map["general"])
    return prompt + (
        " 상품의 색상, 디자인, 로고, 디테일을 100% 정확하게 유지해주세요."
        " 파리 하이패션 에디토리얼 스타일, AI 느낌이 나지 않는 실제 화보처럼."
        " 매우 중요: 배경색이 이미지 전체 캔버스를 빈틈없이 채워야 합니다. 흰색 여백, 테두리, 빈 공간이 절대 없어야 합니다."
        " 절대 금지: 원본 상품 이미지에 없는 로고, 텍스트, 스폰서명, 브랜드 마크를 추가하지 마세요."
        " 원본에 보이는 것만 정확히 재현하고, 당신이 알고 있는 지식으로 없는 요소를 추가하지 마세요."
    )


# 성별·연령 키워드 → 프리셋 그룹 매핑
_GENDER_AGE_KEYWORDS: list[tuple[list[str], str]] = [
    # 키즈 우선 (키즈 여아 > 키즈 남아 > 키즈 공용)
    (["키즈여아", "여아", "걸즈", "girls", "girl"], "kids_girl"),
    (["키즈남아", "남아", "보이즈", "boys", "boy"], "kids_boy"),
    (
        ["키즈", "아동", "주니어", "유아", "어린이", "kids", "junior", "child"],
        "kids_girl",
    ),  # 성별 불명 시 여아 기본
    # 성인
    (
        [
            "여성",
            "우먼",
            "우먼스",
            "레이디",
            "women",
            "woman",
            "ladies",
            "wmns",
            "여자",
        ],
        "female",
    ),
    (["남성", "맨즈", "멘즈", "men", "man", "남자"], "male"),
    (["남녀공용", "유니섹스", "unisex", "공용"], "female"),  # 공용은 여성 기본
]


def _detect_gender_age_from_text(
    category: str, name: str, brand: str = ""
) -> str | None:
    """카테고리 + 상품명 텍스트에서 성별·연령 그룹 판별.

    반환: 'female' | 'male' | 'kids_girl' | 'kids_boy' | None (판별 불가)
    """
    text = f"{category} {name} {brand}".lower().replace(" ", "")
    for keywords, group in _GENDER_AGE_KEYWORDS:
        for kw in keywords:
            if kw.lower().replace(" ", "") in text:
                return group
    return None


def _pick_preset_from_group(group: str) -> str:
    """그룹명으로 프리셋 키 랜덤 선택."""
    import random

    presets = [k for k in MODEL_PRESETS if k.startswith(group)]
    return random.choice(presets) if presets else "female_v1"


class ImageTransformService:
    """이미지 변환 서비스 — rembg(배경제거) + FLUX(착용컷/연출컷) + R2/로컬 저장."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _get_setting(self, key: str) -> dict[str, Any] | None:
        from backend.domain.samba.forbidden.repository import SambaSettingsRepository

        repo = SambaSettingsRepository(self.session)
        row = await repo.find_by_async(key=key)
        if row and isinstance(row.value, dict):
            return row.value
        return None

    async def _get_gemini_config(self) -> tuple[str, str]:
        """Gemini API 키, 모델 반환 (이미지 변환 / AI태그)."""
        creds = await self._get_setting("gemini")
        if not creds:
            raise ValueError(
                "Gemini AI 설정이 없습니다. 설정 페이지에서 API Key를 입력하세요."
            )
        api_key = str(creds.get("apiKey", "")).strip()
        model = str(creds.get("model", "gemini-2.5-flash"))
        if not api_key:
            raise ValueError("Gemini API Key가 비어있습니다.")
        return api_key, model

    async def _get_r2_client(self) -> tuple[Any, str, str] | None:
        """R2 설정이 있으면 boto3 클라이언트 반환, 없으면 None."""
        creds = await self._get_setting("cloudflare_r2")
        if not creds:
            return None
        account_id = str(creds.get("accountId", "")).strip()
        access_key = str(creds.get("accessKey", "")).strip()
        secret_key = str(creds.get("secretKey", "")).strip()
        bucket_name = str(creds.get("bucketName", "")).strip()
        public_url = str(creds.get("publicUrl", "")).strip().rstrip("/")
        if not access_key or not secret_key or not bucket_name:
            return None
        try:
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name="auto",
            )
            return client, bucket_name, public_url
        except Exception:
            return None

    # NAT 데이터 요금 절감: 다운로드 최대 크기 5MB (고해상도 원본 차단)
    _MAX_DOWNLOAD_SIZE = 5 * 1024 * 1024

    async def _download_image(
        self,
        url: str,
        client: httpx.AsyncClient | None = None,
        max_size: int | None = None,
    ) -> bytes:
        """이미지 URL에서 바이트 다운로드 (실패 시 1회 재시도).

        max_size: 다운로드 허용 최대 바이트. None이면 _MAX_DOWNLOAD_SIZE(5MB) 사용.
        1038 방어망(mirror_oversized_to_r2)처럼 큰 원본이라도 리사이즈해서
        마켓에 미러해야 하는 경우는 호출부에서 20MB 등 더 큰 값을 전달한다.
        """
        import asyncio
        from urllib.parse import urlparse

        cap = max_size if (max_size is not None) else self._MAX_DOWNLOAD_SIZE

        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        if "msscdn.net" in (parsed.netloc or ""):
            referer = "https://www.musinsa.com/"
        elif "fashionplus" in (parsed.netloc or ""):
            referer = "https://www.fashionplus.co.kr/"

        _headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer,
        }

        async def _do_get(c: httpx.AsyncClient) -> bytes:
            for attempt in range(2):
                try:
                    # Content-Length 사전 체크로 대용량 원본 바로 차단
                    async with c.stream("GET", url, headers=_headers) as resp:
                        resp.raise_for_status()
                        cl = resp.headers.get("content-length")
                        if cl and int(cl) > cap:
                            raise ValueError(f"이미지 용량 초과({int(cl)}B > {cap}B)")
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            total += len(chunk)
                            if total > cap:
                                raise ValueError(f"이미지 용량 초과(스트림 {total}B)")
                            chunks.append(chunk)
                        content = b"".join(chunks)
                    if len(content) < 1000:
                        raise ValueError(f"이미지가 비정상적으로 작음({len(content)}B)")
                    return content
                except Exception:
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    raise
            raise RuntimeError("unreachable")

        if client:
            return await _do_get(client)
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            return await _do_get(c)

    @staticmethod
    def _is_product_image(url: str, image_bytes: bytes | None = None) -> bool:
        """URL 패턴 + 이미지 비율로 상품 사진 여부 판별.

        배너, 로고, 브랜드 소개 등 비상품 이미지를 걸러낸다.
        """
        url_lower = url.lower()

        # URL 패턴 필터 — 배너/로고/광고 이미지 제외
        skip_patterns = [
            "brand_intro",
            "brand_logo",
            "brand_banner",
            "ad_brand",
            "ad_logo",
            "ad_banner",
            "/banner/",
            "/logo/",
            "/event/",
            "/promotion/",
            "/ad/",
            "/ads/",
            "/advert/",
            "logo_",
            "banner_",
            "btn_",
            "icon_",
            "size_guide",
            "sizeguide",
            "size_chart",
            "delivery_info",
            "shipping_info",
            "notice",
            "caution",
            "warning",
        ]
        for pat in skip_patterns:
            if pat in url_lower:
                return False

        # 이미지 비율 + 콘텐츠 체크
        if image_bytes and len(image_bytes) > 100:
            try:
                from PIL import Image

                img = Image.open(io.BytesIO(image_bytes))
                w, h = img.size
                if w > 0 and h > 0:
                    ratio = w / h
                    # 가로가 3배 이상 넓으면 배너 (예: 1200x200)
                    if ratio > 3.0:
                        return False
                    # 세로가 5배 이상 길면 안내 이미지
                    if ratio < 0.2:
                        return False
                    # 너무 작은 이미지 (아이콘 등)
                    if w < 100 or h < 100:
                        return False

                    # 색상 다양성 체크 — 로고/아이콘은 색이 극히 적음
                    small = img.convert("RGB").resize((50, 50))
                    colors = len(set(small.getdata()))
                    # 50x50=2500픽셀 중 고유색 30개 미만 → 로고/단색 이미지
                    if colors < 30:
                        return False
            except Exception:
                pass

        return True

    async def _resolve_preset_for_product(
        self,
        product: Any,
    ) -> tuple[str, str, bytes | None]:
        """상품별 최적 프리셋 자동 결정 (텍스트 기반). (preset_key, model_desc, ref_image) 반환."""
        category = " > ".join(
            filter(
                None,
                [
                    getattr(product, "category1", ""),
                    getattr(product, "category2", ""),
                    getattr(product, "category3", ""),
                ],
            )
        )
        name = product.name or ""
        brand = product.brand or ""

        # 카테고리 + 상품명으로 판별 (판별 불가 시 여성 기본)
        group = _detect_gender_age_from_text(category, name, brand) or "female"

        preset_key = _pick_preset_from_group(group)
        preset = MODEL_PRESETS[preset_key]
        ref_image = await self._load_preset_image(preset_key)
        logger.info(
            f"[이미지] {product.id} 자동 프리셋: {preset_key} ({preset['label']})"
        )
        return preset_key, preset["desc"], ref_image

    async def _load_preset_image(self, preset_key: str) -> bytes | None:
        """프리셋 참조 이미지 로드 — 로컬 → R2 CDN 순서로 fallback."""
        preset = MODEL_PRESETS.get(preset_key)
        if not preset or not preset.get("image"):
            return None

        filename = preset["image"]

        # 1) 로컬 파일 확인
        local_path = PRESET_IMAGE_DIR / filename
        if local_path.exists():
            logger.info(f"[프리셋] 로컬에서 로드: {local_path}")
            return local_path.read_bytes()

        # 2) R2 CDN에서 다운로드
        r2 = await self._get_r2_client()
        if r2:
            _, _, public_url = r2
            cdn_url = f"{public_url}/model_presets/{filename}"
            try:
                async with httpx.AsyncClient(
                    timeout=30, follow_redirects=True
                ) as client:
                    resp = await client.get(cdn_url)
                    resp.raise_for_status()
                    if len(resp.content) > 1000:
                        logger.info(f"[프리셋] R2 CDN에서 로드: {cdn_url}")
                        return resp.content
            except Exception as e:
                logger.warning(f"[프리셋] R2 CDN 다운로드 실패: {e}")

        # 3) 참조 이미지 없음 — 텍스트 프롬프트만 사용
        logger.warning(f"[프리셋] 참조 이미지 없음 ({filename}), 텍스트만 사용")
        return None

    async def _remove_background_rembg(self, image_bytes: bytes) -> bytes:
        """rembg로 배경 제거 (로컬 실행, API 비용 ₩0)."""
        import asyncio as _aio
        from functools import partial
        from PIL import Image
        from rembg import remove

        def _process(data: bytes) -> bytes:
            import numpy as np

            # 큰 이미지 리사이즈 (메모리/시간 절약)
            src = Image.open(io.BytesIO(data)).convert("RGB")
            if max(src.size) > 1024:
                src.thumbnail((1024, 1024), Image.LANCZOS)
            buf_resized = io.BytesIO()
            src.save(buf_resized, format="PNG")
            data = buf_resized.getvalue()
            # 원본 네 모서리 16x16 블록을 평균내어 배경색 추정
            # (흰 사진이면 흰색, 회색 스튜디오 컷이면 회색으로 자연 합성)
            arr_src = np.array(src)
            h, w = arr_src.shape[:2]
            sz = max(8, min(16, h // 32, w // 32))
            corners = np.concatenate(
                [
                    arr_src[:sz, :sz].reshape(-1, 3),
                    arr_src[:sz, -sz:].reshape(-1, 3),
                    arr_src[-sz:, :sz].reshape(-1, 3),
                    arr_src[-sz:, -sz:].reshape(-1, 3),
                ],
                axis=0,
            )
            bg_color = tuple(int(c) for c in np.median(corners, axis=0))
            # 캐시된 세션 재사용 (매번 모델 로드 방지)
            session = _get_rembg_session()
            result = remove(
                data,
                session=session,
                alpha_matting=True,
                alpha_matting_foreground_threshold=250,
                alpha_matting_background_threshold=30,
                alpha_matting_erode_size=15,
            )
            # 부드러운 알파 그대로 사용 — 애매한 영역(팔/머리 가장자리)은
            # 자르지 않고 원본 배경색과 자연스럽게 블렌드.
            # 마켓 API(11번가/스마트스토어/롯데ON 등)가 WebP를 거부하므로 JPEG로 통일.
            img = Image.open(io.BytesIO(result)).convert("RGBA")
            bg = Image.new("RGBA", img.size, (*bg_color, 255))
            composite = Image.alpha_composite(bg, img).convert("RGB")
            buf = io.BytesIO()
            composite.save(buf, format="JPEG", quality=92, optimize=True)
            return buf.getvalue()

        # CPU 작업이므로 스레드풀에서 실행 (이벤트루프 블로킹 방지)
        return await _aio.to_thread(partial(_process, image_bytes))

    @staticmethod
    def _detect_mime(data: bytes) -> str:
        """이미지 바이트에서 MIME 타입 감지."""
        if data[:4] == b"\x89PNG":
            return "image/png"
        if data[:4] == b"RIFF":
            return "image/webp"
        return "image/jpeg"

    @staticmethod
    def _is_same_image(img_a: bytes, img_b: bytes) -> bool:
        """두 이미지가 동일/유사한지 판별 (축소 후 픽셀 비교)."""
        if img_a == img_b:
            return True
        if not img_a or not img_b:
            return False
        try:
            from PIL import Image

            a = Image.open(io.BytesIO(img_a)).convert("RGB").resize((32, 32))
            b = Image.open(io.BytesIO(img_b)).convert("RGB").resize((32, 32))
            pixels_a = list(a.getdata())
            pixels_b = list(b.getdata())
            total_diff = sum(
                abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) + abs(pa[2] - pb[2])
                for pa, pb in zip(pixels_a, pixels_b)
            )
            avg_diff = total_diff / (32 * 32 * 3)
            return avg_diff < 15
        except Exception:
            ratio = abs(len(img_a) - len(img_b)) / max(len(img_a), len(img_b))
            return ratio < 0.03 and img_a[:2048] == img_b[:2048]

    async def _transform_image_gemini(
        self,
        api_key: str,
        model: str,
        image_bytes: bytes,
        prompt: str,
        ref_image_bytes: bytes | None = None,
        design_ref_bytes: bytes | None = None,
    ) -> bytes:
        """Gemini API로 이미지 변환.

        ref_image_bytes: 모델 프리셋 참조 이미지
        design_ref_bytes: 대표이미지 (디자인 기준 — 추가이미지 변환 시 사용)
        """
        import base64

        parts: list[dict[str, Any]] = []

        if ref_image_bytes:
            main_prompt = (
                "첫 번째 이미지는 반드시 착용/사용해야 할 상품입니다. "
                "이 상품의 색상, 로고, 패턴, 텍스트, 디자인을 100% 정확하게 재현하세요. "
                "절대로 상품의 디자인을 변경하거나 다른 옷으로 대체하지 마세요. "
                "절대 금지: 원본 이미지에 없는 로고, 텍스트, 스폰서명을 추가하지 마세요. "
                "당신이 알고 있는 브랜드 지식으로 없는 요소를 만들어내지 마세요. 보이는 것만 재현하세요. "
            )
            last_img_label = (
                "마지막 이미지는" if design_ref_bytes else "두 번째 이미지는"
            )
            main_prompt += (
                f"\n[중요: 모델 교체 규칙]\n"
                "첫 번째 이미지에 사람/모델이 있다면, 그 사람의 얼굴, 헤어스타일, 피부색, 체형을 모두 무시하세요. "
                "오직 그 사람이 입고 있는 '옷/상품'만 추출하세요. "
                f"{last_img_label} 새로운 모델의 참조입니다. "
                "생성할 이미지의 모델은 반드시 이 참조 이미지의 얼굴, 헤어스타일, 피부색, 체형을 사용하세요. "
                "원본 상품 이미지에 있던 모델의 외모는 절대 사용하지 마세요. 완전히 다른 사람으로 교체하는 것입니다. "
                "모델은 반드시 백인 서양인(Caucasian)이어야 합니다. "
                "반드시 첫 번째 이미지의 상품을 착용한 사진을 생성해야 합니다. "
                "첫 번째 이미지가 앞면이면 앞면 착용컷, 뒷면이면 뒷면 착용컷을 생성하세요. 이미지의 각도를 그대로 존중하세요. "
                "배경이 캔버스 전체를 빈틈없이 채워야 하며, 흰색 테두리나 여백이 절대 없어야 합니다. "
            )
            if design_ref_bytes:
                main_prompt += (
                    "두 번째 이미지는 동일 상품의 대표 이미지입니다. 상품의 전체 디자인(색상, 브랜드, 소재감) 참고용으로만 사용하세요. "
                    "단, 각도는 반드시 첫 번째 이미지를 따르세요. "
                )
            parts.append({"text": main_prompt + prompt})

            # 1) 상품 이미지 (최우선)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": self._detect_mime(image_bytes),
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )
            # 2) 디자인 기준 대표이미지 (있으면)
            if design_ref_bytes:
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": self._detect_mime(design_ref_bytes),
                            "data": base64.b64encode(design_ref_bytes).decode("ascii"),
                        }
                    }
                )
            # 3) 모델 프리셋 (얼굴/체형만 참고)
            parts.append(
                {
                    "inline_data": {
                        "mime_type": self._detect_mime(ref_image_bytes),
                        "data": base64.b64encode(ref_image_bytes).decode("ascii"),
                    }
                }
            )
        else:
            parts.append({"text": prompt})
            parts.append(
                {
                    "inline_data": {
                        "mime_type": self._detect_mime(image_bytes),
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )

        body = {
            "contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

        import asyncio as _aio_gemini

        async with httpx.AsyncClient(timeout=120) as client:
            # 429 rate limit 대비 최대 3회 재시도
            for attempt in range(3):
                resp = await client.post(
                    url, json=body, headers={"Content-Type": "application/json"}
                )
                if resp.status_code == 429:
                    if attempt < 2:
                        wait = 30 * (attempt + 1)
                        logger.warning(
                            f"[이미지] Gemini 429 rate limit — {wait}초 대기 후 재시도"
                        )
                        await _aio_gemini.sleep(wait)
                        continue
                resp.raise_for_status()
                break

            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError("Gemini 응답에 candidates 없음")

            parts_resp = candidates[0].get("content", {}).get("parts", [])
            for part in parts_resp:
                if "inlineData" in part:
                    result = base64.b64decode(part["inlineData"]["data"])
                    # 참조 프리셋 이미지가 그대로 반환된 경우 거부
                    if ref_image_bytes and self._is_same_image(result, ref_image_bytes):
                        raise ValueError(
                            "Gemini가 참조 프리셋 이미지를 그대로 반환 — 변환 실패"
                        )
                    return result

            raise ValueError("Gemini 응답에 이미지 없음")

    async def _save_image(self, image_bytes: bytes, original_url: str) -> str:
        """R2 또는 로컬에 이미지 저장 후 URL 반환.

        파일명을 결정적(content-hash)으로 생성하여 동일 바이트는 동일 경로에 저장한다.
        R2에 이미 존재하면 업로드를 생략하여 NAT egress 비용을 절감한다.

        [중요] Gemini API는 PNG 바이트를 반환하지만 파일명/MIME 은 .jpg/image/jpeg 로
        통일하므로 실제 magic bytes 가 Content-Type 과 불일치하게 된다.
        롯데홈쇼핑 등 일부 마켓 서버는 외부 fetch 후 magic bytes 검증에서 거부하므로
        반드시 PIL 로 JPEG 변환 후 업로드한다. 투명 채널은 흰 배경으로 합성.
        """
        # 매직바이트 ≠ Content-Type 불일치 방지 + 마켓 호환을 위한 사양 정규화:
        # 1) JPEG 강제 변환 (PNG 등 다른 매직바이트가 .jpg 로 저장되는 문제 차단)
        # 2) 1000x1000 미만이면 비율 유지 upscale (롯데홈쇼핑 등 최소 해상도 미달로
        #    대표/추가이미지가 placeholder 로 대체되는 문제 해결)
        # 3) 정사각형 아니면 흰 배경 padding (마켓별 정사각형 요구 호환)
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(image_bytes))
            if img.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                bg.paste(rgba, mask=rgba.split()[3])
                img = bg
            elif img.mode == "P":
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[3])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            target = 1000
            W, H = img.size
            if max(W, H) < target:
                scale = target / max(W, H)
                img = img.resize((round(W * scale), round(H * scale)), Image.LANCZOS)
                W, H = img.size
            if W != H:
                side = max(W, H)
                canvas = Image.new("RGB", (side, side), (255, 255, 255))
                canvas.paste(img, ((side - W) // 2, (side - H) // 2))
                img = canvas

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=92, optimize=True)
            image_bytes = buf.getvalue()
        except Exception as e:
            logger.warning(
                f"[이미지] JPEG/사이즈 정규화 실패 — 원본 바이트 그대로 저장 시도: {e}"
            )

        # content-hash 기반 결정적 파일명 (중복 업로드 방지)
        content_hash = hashlib.md5(image_bytes).hexdigest()[:16]
        filename = f"ai_{content_hash}.jpg"
        key = f"transformed/{filename}"

        # 마켓 서버 fetch 호환을 위한 R2 ExtraArgs:
        # - ContentType: image/jpeg (실제 magic bytes 와 일치)
        # - ContentDisposition: inline + 명시적 .jpg 파일명 (롯데홈 등 일부 마켓이
        #   attachment 응답을 거부하거나 확장자 파싱 실패하는 케이스 방지)
        # - CacheControl: 마켓 서버 측 캐시 친화적 응답
        extra_args = {
            "ContentType": "image/jpeg",
            "ContentDisposition": f'inline; filename="{filename}"',
            "CacheControl": "public, max-age=31536000",
        }

        # R2 저장 시도
        r2 = await self._get_r2_client()
        if r2:
            client, bucket_name, public_url = r2
            try:
                import asyncio as _aio
                from functools import partial

                # HeadObject로 기존 객체 확인 — 존재 시 메타데이터만 갱신
                # (기존 PNG 매직바이트로 잘못 저장된 객체는 새 content_hash 가 되므로
                #  자연스럽게 신규 객체로 업로드됨)
                def _exists() -> bool:
                    try:
                        client.head_object(Bucket=bucket_name, Key=key)
                        return True
                    except Exception:
                        return False

                if await _aio.to_thread(_exists):
                    return f"{public_url}/{key}"

                await _aio.to_thread(
                    partial(
                        client.upload_fileobj,
                        io.BytesIO(image_bytes),
                        bucket_name,
                        key,
                        ExtraArgs=extra_args,
                    ),
                )
                return f"{public_url}/{key}"
            except Exception as e:
                logger.warning(f"[이미지] R2 업로드 실패, 로컬 저장으로 전환: {e}")

        # 로컬 저장
        local_path = LOCAL_IMAGE_DIR / filename
        local_path.write_bytes(image_bytes)
        # 절대 URL 반환 (프론트에서 직접 접근 가능하도록)
        from backend.core.config import settings

        base_url = getattr(settings, "backend_url", "") or os.environ.get(
            "BACKEND_URL", "http://localhost:28080"
        )
        return f"{base_url}/static/images/{filename}"

    # ── 프로세스 레벨 미러 캐시 (원본URL → R2URL) ──────────────────────
    # 같은 워커 프로세스가 동일 상품을 N개 마켓 등록할 때 동일 이미지를 N번
    # 다운로드+JPEG재인코딩+업로드하던 회귀를 차단. 키는 정규화한 원본 URL,
    # 값은 미러링 성공 시 반환된 R2 publicUrl. R2 키가 content-hash 기반이라
    # 같은 URL이 같은 R2 객체로 결정적으로 매핑되므로 캐시 사용이 안전.
    # 영속 캐시(DB 컬럼)는 별도 작업으로 분리 — 여기는 프로세스 수명만 보장.
    _R2_MIRROR_CACHE: dict[str, str] = {}
    _R2_MIRROR_CACHE_MAX = 20000

    # 외부 CDN(referer 차단) → R2 미러링: 마켓 등록 시 핫링크 워터마크 방지용
    # 무신사 image.msscdn.net 등은 외부 도메인이 fetch하면 워터마크 응답
    _HOTLINK_BLOCKED_HOSTS = (
        "msscdn.net",
        "image.musinsa.com",
        # 롯데온 CDN — HEAD 403/비표준 path(`/dims/optimize/resizemc/...`)로
        # 11번가 등록 시 "기본이미지 존재하지 않음" 에러 유발 → R2 선미러 필요
        "contents.lotteon.com",
        # GS샵 CDN — 11번가가 fetch 시 호스트 차단/확장자 누락으로 "기본이미지 없음"
        # 500 에러 유발 → R2 선미러 필요
        # 실제 GS샵 메인 이미지 CDN은 asset.m-gs.kr / static.m-gs.kr 사용
        "asset.m-gs.kr",
        "static.m-gs.kr",
        # 무신사 상세에 박히는 브랜드 자체 CDN — SSG 등 서버 fetch 마켓이 직접
        # 다운로드 못 해 "파일 다운로드 도중 오류" 등록거부 유발(프로덕션 987건).
        # 우리 서버는 다운로드 가능(검증) → R2 선미러 필요.
        "puma.net",
        "skecherskorea.co.kr",
        "leecom01.kr",
        "innerplan.co.kr",
        "yswholesale.com",
        "cloudinary.com",
    )

    async def mirror_external_to_r2(
        self, urls: list[str], min_bytes: int = 0
    ) -> tuple[list[str], dict[str, str]]:
        """차단 도메인의 이미지 URL을 R2로 미러링하여 R2 URL로 치환.

        - msscdn.net 등 referer/hotlink 차단 도메인만 다운로드 후 R2 업로드
        - min_bytes>0 이면 차단 도메인이 아니어도 URL 바이트 길이가 그 값을 초과하는
          이미지는 미러링(짧은 R2 URL로 단축). 롯데ON origImgFileNm 200byte 한도처럼
          긴 URL(인코딩된 한글 파일명 등)이 거부되는 경우 대응.
        - 이미 R2 publicUrl 도메인이거나 (차단 대상도 아니고 min_bytes도 안 넘으면) 원본 유지
        - 다운로드/업로드 실패 시 해당 URL은 결과에서 제외(드롭)
        - 원본 포맷(MIME) 보존: webp 변환 없이 그대로 저장

        반환:
            - list[str]: 치환 결과 URL 리스트 (실패는 드롭)
            - dict[str, str]: (원본 → R2) 매핑. 실제 미러링된 항목만 포함.
              상세 HTML 문자열 안의 차단 URL을 일괄 치환할 때 사용.
        """
        from urllib.parse import urlparse

        if not urls:
            return [], {}

        r2 = await self._get_r2_client()
        if not r2:
            # R2 설정이 없으면 미러 불가 — 원본 그대로 반환, 매핑 없음
            return list(urls), {}
        client, bucket_name, public_url = r2
        public_host = urlparse(public_url).netloc if public_url else ""

        # 결과 슬롯 — 입력 순서 보존
        result_slots: list[str | None] = [None] * len(urls)
        url_map: dict[str, str] = {}

        # 1차 패스: 캐시/패스스루/차단대상 분류
        to_download: list[tuple[int, str]] = []  # (slot_idx, url)
        for _i, url in enumerate(urls):
            if not url:
                continue
            # 프로세스 캐시 hit — 다운로드/재인코딩/업로드 전부 skip
            _cached = self._R2_MIRROR_CACHE.get(url)
            if _cached:
                result_slots[_i] = _cached
                url_map[url] = _cached
                continue
            parsed = urlparse(url)
            host = (parsed.netloc or "").lower()
            # 이미 R2(publicUrl) 호스트면 그대로 사용
            if public_host and host == public_host:
                result_slots[_i] = url
                continue
            # 차단 도메인이 아니고 min_bytes도 안 넘으면 원본 유지
            _blocked = any(b in host for b in self._HOTLINK_BLOCKED_HOSTS)
            _too_long = bool(min_bytes) and len(url.encode("utf-8")) > min_bytes
            if not _blocked and not _too_long:
                result_slots[_i] = url
                continue
            to_download.append((_i, url))

        # 2차 패스: 차단 도메인 병렬 미러링 (Semaphore로 1CPU/네트워크 부하 제어)
        if to_download:
            _sem = asyncio.Semaphore(4)

            async def _mirror_one(_idx: int, _url: str) -> tuple[int, str, str | None]:
                async with _sem:
                    try:
                        parsed = urlparse(_url)
                        host = (parsed.netloc or "").lower()
                        fetch_url = _url
                        if "msscdn.net" in host:
                            fetch_url = re.sub(r"_\d{3,4}\.jpg$", "_big.jpg", _url)
                        try:
                            image_bytes = await self._download_image(fetch_url)
                        except Exception:
                            # msscdn _big.jpg 고해상도 변형이 없는 상품(404 등)은
                            # 원본 해상도 URL로 폴백. 폴백 없으면 메인이미지가 전부
                            # 드롭돼 11번가 "미러링 후 이미지없음"으로 등록 거부됨
                            # (프로덕션 583건, 원본 _500.jpg는 100% 다운로드 가능 검증).
                            if fetch_url != _url:
                                image_bytes = await self._download_image(_url)
                            else:
                                raise
                        # 11번가 등 일부 마켓은 webp 거부 + magic bytes 검증 수행
                        # → PIL로 무조건 JPEG 변환 후 업로드 (AI 가공 _save_image와 동일)
                        try:
                            from PIL import Image

                            _img = Image.open(io.BytesIO(image_bytes))
                            if _img.mode in ("RGBA", "LA", "P"):
                                _bg = Image.new("RGB", _img.size, (255, 255, 255))
                                _rgba = _img.convert("RGBA")
                                _bg.paste(_rgba, mask=_rgba.split()[3])
                                _img = _bg
                            elif _img.mode != "RGB":
                                _img = _img.convert("RGB")
                            _buf = io.BytesIO()
                            _img.save(_buf, format="JPEG", quality=92, optimize=True)
                            image_bytes = _buf.getvalue()
                            mime = "image/jpeg"
                            ext = "jpg"
                        except Exception as _e:
                            logger.warning(
                                f"[이미지미러] JPEG 정규화 실패, 원본 유지: {_url} — {_e}"
                            )
                            mime = self._detect_mime(image_bytes)
                            ext = {
                                "image/png": "png",
                                "image/webp": "webp",
                                "image/jpeg": "jpg",
                            }.get(mime, "jpg")
                        content_hash = hashlib.md5(image_bytes).hexdigest()[:16]
                        key = f"mirror/{content_hash}.{ext}"

                        def _exists(_key: str = key) -> bool:
                            try:
                                client.head_object(Bucket=bucket_name, Key=_key)
                                return True
                            except Exception:
                                return False

                        if not await asyncio.to_thread(_exists):
                            await asyncio.to_thread(
                                partial(
                                    client.upload_fileobj,
                                    io.BytesIO(image_bytes),
                                    bucket_name,
                                    key,
                                    ExtraArgs={"ContentType": mime},
                                ),
                            )
                        return (_idx, _url, f"{public_url}/{key}")
                    except Exception as e:
                        logger.warning(f"[이미지미러] 실패로 드롭: {_url} — {e}")
                        return (_idx, _url, None)

            _gathered = await asyncio.gather(
                *(_mirror_one(_i, _u) for _i, _u in to_download)
            )
            for _idx, _src_url, _mirror_url in _gathered:
                if _mirror_url is None:
                    continue
                result_slots[_idx] = _mirror_url
                url_map[_src_url] = _mirror_url
                # 캐시 적재 (사이즈 캡 — 단순 FIFO drop)
                if len(self._R2_MIRROR_CACHE) >= self._R2_MIRROR_CACHE_MAX:
                    try:
                        self._R2_MIRROR_CACHE.pop(next(iter(self._R2_MIRROR_CACHE)))
                    except StopIteration:
                        pass
                self._R2_MIRROR_CACHE[_src_url] = _mirror_url

        result = [x for x in result_slots if x is not None]
        return result, url_map

    async def mirror_with_persistence(
        self, product_id: str | None, urls: list[str], min_bytes: int = 0
    ) -> tuple[list[str], dict[str, str]]:
        """DB 영속 매핑(samba_collected_product.image_mirror_map) 활용 + 신규 매핑 저장.

        - product_id 없으면 일반 mirror_external_to_r2 와 동일 (DB 미관여)
        - DB의 기존 매핑을 프로세스 캐시에 시드해 즉시 hit
        - 미러링 후 신규 매핑만 DB에 머지 저장 (commit 포함)
        - 실패 시 일반 미러 결과만 반환 (DB 오류는 로깅 후 무시 — 등록 자체는 진행)
        """
        if not urls or not product_id:
            return await self.mirror_external_to_r2(urls, min_bytes=min_bytes)

        from sqlalchemy import select, update

        from backend.domain.samba.collector.model import SambaCollectedProduct

        _db_map: dict[str, str] = {}
        try:
            _row = (
                await self.session.execute(
                    select(SambaCollectedProduct.image_mirror_map).where(
                        SambaCollectedProduct.id == product_id
                    )
                )
            ).first()
            if _row and _row[0]:
                _db_map = dict(_row[0])
        except Exception as e:
            logger.warning(f"[이미지미러] DB 매핑 로드 실패 — 캐시만 사용: {e}")

        # DB 매핑을 프로세스 캐시에 시드 (배포 직후 첫 미러도 다운로드 0회 달성)
        for _k, _v in _db_map.items():
            self._R2_MIRROR_CACHE.setdefault(_k, _v)

        _result, _url_map = await self.mirror_external_to_r2(urls, min_bytes=min_bytes)

        # 신규(또는 변경) 매핑만 DB에 머지
        _new = {k: v for k, v in _url_map.items() if _db_map.get(k) != v}
        if _new:
            _merged = {**_db_map, **_new}
            try:
                await self.session.execute(
                    update(SambaCollectedProduct)
                    .where(SambaCollectedProduct.id == product_id)
                    .values(image_mirror_map=_merged)
                )
                await self.session.commit()
            except Exception as e:
                logger.warning(
                    f"[이미지미러] DB 매핑 저장 실패 — 다음 등록 시 재시도: {e}"
                )
                try:
                    await self.session.rollback()
                except Exception:
                    pass

        return _result, _url_map

    @staticmethod
    def is_hotlink_blocked_url(url: str) -> bool:
        """detail_html 문자열에서 차단 도메인 URL을 식별할 때 사용."""
        if not url:
            return False
        from urllib.parse import urlparse

        host = (urlparse(url).netloc or "").lower()
        return any(
            blocked in host for blocked in ImageTransformService._HOTLINK_BLOCKED_HOSTS
        )

    async def mirror_oversized_to_r2(
        self,
        urls: list[str],
        max_bytes: int = 900_000,
        max_dim: int = 1500,
        quality: int = 85,
        min_dim: int = 0,
        enforce_max_dim: bool = False,
    ) -> tuple[list[str], dict[str, str], set[str]]:
        """용량/픽셀 초과·미달 이미지를 다운로드/리사이즈하여 R2로 업로드.

        롯데홈쇼핑 [1038] 등 마켓 측 이미지 용량 한도 대응 — 호스트 무관하게
        max_bytes 초과로 추정되거나 실제 다운로드 크기가 초과인 경우만 R2 미러.
        msscdn 등 차단 도메인은 mirror_external_to_r2 가 별도로 처리하므로 중복 미러 안 함.

        쿠팡 검증(500x500~5000x5000) 대응:
        - min_dim > 0 또는 enforce_max_dim=True 이면 HEAD 분기 우회하고 무조건 다운로드 →
          PIL 로 픽셀 크기 확인 → min_dim 미만이면 LANCZOS 업스케일, max_dim 초과면 다운스케일.
        - 기본값(min_dim=0, enforce_max_dim=False)은 기존 동작 그대로 (1038/lottehome 무영향).

        반환: (치환 결과 URL 리스트, 원본→R2 매핑)
        """
        from urllib.parse import urlparse

        if not urls:
            return [], {}, set()
        r2 = await self._get_r2_client()
        if not r2:
            return list(urls), {}, set()
        client_r2, bucket_name, public_url = r2
        public_host = urlparse(public_url).netloc if public_url else ""

        result: list[str] = []
        url_map: dict[str, str] = {}
        failed: set[str] = set()

        # 빠른 사전 체크용 HTTP 클라이언트 (HEAD)
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=True
        ) as http_client:
            for url in urls:
                if not url:
                    continue
                try:
                    parsed = urlparse(url)
                    host = (parsed.netloc or "").lower()
                    # R2 본인 호스트면 그대로
                    if public_host and host == public_host:
                        result.append(url)
                        continue
                    # min_dim/enforce_max_dim 모드: HEAD 우회하고 무조건 다운로드
                    # — HEAD 로는 픽셀 크기를 알 수 없으므로 PIL 로 직접 확인 필요.
                    strict_pixel = bool(min_dim > 0 or enforce_max_dim)

                    # HEAD 로 size 조회 — 초과 후보만 다운로드
                    # Content-Length 누락/비숫자 또는 HEAD 자체 실패 시 over=True 로 fallthrough
                    # (msscdn 등 일부 CDN은 HEAD 에 CL 미반환 → 사전 스킵되던 [1038] 재발 원인)
                    over = False
                    if not strict_pixel:
                        try:
                            head = await http_client.head(url)
                            cl = head.headers.get("content-length", "")
                            if cl.isdigit():
                                over = int(cl) > max_bytes
                            else:
                                over = True
                        except Exception:
                            # HEAD 실패 → 일단 다운로드 후 판단
                            over = True

                        if not over:
                            result.append(url)
                            continue

                    # 다운로드 후 PIL 로 리사이즈
                    # 1038 방어망용 — 5MB 기본 가드를 20MB까지 풀어줘야
                    # 도매 CDN(leecom01 등) 6~10MB 원본도 미러 가능
                    image_bytes = await self._download_image(
                        url, max_size=20 * 1024 * 1024
                    )
                    if not image_bytes:
                        failed.add(url)
                        result.append(url)
                        continue

                    from PIL import Image  # noqa: F811

                    img = Image.open(io.BytesIO(image_bytes))
                    img = img.convert("RGB")
                    w, h = img.size

                    # 변경 필요 여부 판단
                    need_upscale = bool(min_dim > 0 and min(w, h) < min_dim)
                    need_downscale = bool(
                        (enforce_max_dim or not strict_pixel) and max(w, h) > max_dim
                    )
                    over_bytes = len(image_bytes) > max_bytes
                    # 핫링크 차단 도메인 — 크기/용량 무관하게 R2 강제 미러
                    is_hotlink = any(
                        blocked in host for blocked in self._HOTLINK_BLOCKED_HOSTS
                    )

                    # 픽셀/용량 모두 통과하고 차단 도메인도 아니면 원본 유지
                    if (
                        not need_upscale
                        and not need_downscale
                        and not over_bytes
                        and not is_hotlink
                    ):
                        result.append(url)
                        continue

                    # 쿠팡 최소사이즈 검증(500x500) 대응 — 비율 유지하며 LANCZOS 업스케일
                    if need_upscale:
                        ratio = float(min_dim) / float(min(w, h))
                        new_w = max(int(round(w * ratio)), min_dim)
                        new_h = max(int(round(h * ratio)), min_dim)
                        img = img.resize((new_w, new_h), Image.LANCZOS)

                    if max(img.size) > max_dim:
                        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

                    out = io.BytesIO()
                    # 품질 단계적 하향: 85 → 70 → 55 까지 시도
                    final_bytes = b""
                    for q in (quality, 70, 55):
                        out.seek(0)
                        out.truncate(0)
                        img.save(out, format="JPEG", quality=q, optimize=True)
                        final_bytes = out.getvalue()
                        if len(final_bytes) <= max_bytes:
                            break
                    if not final_bytes:
                        result.append(url)
                        continue

                    content_hash = hashlib.md5(final_bytes).hexdigest()[:16]
                    key = f"resized/{content_hash}.jpg"

                    def _exists(_key: str = key) -> bool:
                        try:
                            client_r2.head_object(Bucket=bucket_name, Key=_key)
                            return True
                        except Exception:
                            return False

                    if not await asyncio.to_thread(_exists):
                        await asyncio.to_thread(
                            partial(
                                client_r2.upload_fileobj,
                                io.BytesIO(final_bytes),
                                bucket_name,
                                key,
                                ExtraArgs={"ContentType": "image/jpeg"},
                            ),
                        )
                    mirrored = f"{public_url}/{key}"
                    result.append(mirrored)
                    url_map[url] = mirrored
                    logger.info(
                        f"[이미지리사이즈] {len(image_bytes)}B→{len(final_bytes)}B {url} → {mirrored}"
                    )
                except Exception as e:
                    logger.warning(f"[이미지리사이즈] 실패로 원본 유지: {url} — {e}")
                    failed.add(url)
                    result.append(url)
        return result, url_map, failed

    async def mirror_oversized_in_html(
        self,
        html: str,
        max_bytes: int = 900_000,
        max_dim: int = 1500,
        min_dim: int = 0,
        enforce_max_dim: bool = False,
    ) -> str:
        """HTML 내부 <img src> 중 용량/픽셀 검증을 통과 못한 항목을 리사이즈 후 R2 URL 로 치환.

        쿠팡 검증(500x500~5000x5000) 대응 — min_dim/enforce_max_dim 전달 시 픽셀 보정 포함.
        """
        if not html:
            return html
        import re as _re

        # protocol-relative(//host/...)와 절대(https?://...) 양쪽 매칭
        pattern = _re.compile(r'src=(["\'])((?:https?:)?//[^"\']+)\1', _re.IGNORECASE)
        # orig(원본 문자열) -> normalized(https://...) 매핑
        orig_to_norm: dict[str, str] = {}
        for m in pattern.finditer(html):
            url = m.group(2)
            if url in orig_to_norm:
                continue
            orig_to_norm[url] = ("https:" + url) if url.startswith("//") else url
        if not orig_to_norm:
            return html
        _, url_map, _ = await self.mirror_oversized_to_r2(
            list(orig_to_norm.values()),
            max_bytes=max_bytes,
            max_dim=max_dim,
            min_dim=min_dim,
            enforce_max_dim=enforce_max_dim,
        )
        if not url_map:
            return html
        new_html = html
        for orig, norm in orig_to_norm.items():
            new = url_map.get(norm)
            if new:
                new_html = new_html.replace(orig, new)
        return new_html

    async def mirror_urls_in_html(self, html: str) -> str:
        """HTML 문자열 내부의 차단 도메인 이미지 URL을 R2 미러 URL로 치환.

        detail_html처럼 사전 생성된 HTML 안의 <img src="..."> URL이 미러링을
        우회하여 11번가 서버에 워터마크 응답을 받는 것을 방지.

        protocol-relative URL(`//image.msscdn.net/...`)도 처리 — 무신사
        goodsContents 배너가 `<img src="//image.msscdn.net/...">` 형식이므로
        `https?://`만 매칭하면 미러링 우회되어 핫링크 차단으로 깨진 이미지 노출.
        """
        if not html:
            return html
        import re as _re

        # protocol-relative(//host/...)와 절대(https?://...) 양쪽 매칭
        pattern = _re.compile(r'src=(["\'])((?:https?:)?//[^"\']+)\1', _re.IGNORECASE)
        # orig(원본 문자열) -> normalized(https://...) 매핑
        orig_to_norm: dict[str, str] = {}
        for m in pattern.finditer(html):
            url = m.group(2)
            if url in orig_to_norm:
                continue
            norm = ("https:" + url) if url.startswith("//") else url
            if self.is_hotlink_blocked_url(norm):
                orig_to_norm[url] = norm
        if not orig_to_norm:
            return html

        _, url_map = await self.mirror_external_to_r2(list(orig_to_norm.values()))
        if not url_map:
            return html

        new_html = html
        for orig, norm in orig_to_norm.items():
            new = url_map.get(norm)
            if new:
                new_html = new_html.replace(orig, new)
        return new_html

    async def strip_external_imgs_in_html(self, html: str) -> str:
        """detail_html에서 our-domain/R2가 아닌 <img> 태그를 제거.

        SSG 등 서버 fetch 마켓은 등록 시 detail_html의 모든 <img>를 직접 다운로드
        하는데, 미러링에 실패해 외부 URL이 남으면(puma_notice 깨진 이미지·용량초과
        등) 그 한 장 때문에 상품 전체 등록이 거부된다("파일 다운로드 도중 오류").
        미러 이후 호출해 our-domain/R2가 아닌 <img>를 통째로 제거 → SSG가 fetch
        못 하는 URL을 payload에서 0으로 만든다. 이미 미러된 정상 이미지는 보존.
        data:/상대경로/src 없는 태그는 유지.
        """
        if not html:
            return html
        import re as _re
        from urllib.parse import urlparse as _up

        r2 = await self._get_r2_client()
        public_host = ""
        if r2:
            try:
                public_host = (_up(r2[2]).netloc or "").lower()
            except Exception:
                public_host = ""
        allowed_tokens = [
            t
            for t in (public_host, "samba-wave", "r2.dev", "r2.cloudflarestorage")
            if t
        ]

        def _keep(m: "_re.Match[str]") -> str:
            tag = m.group(0)
            src = _re.search(
                r'src=(["\'])((?:https?:)?//[^"\']+)\1', tag, _re.IGNORECASE
            )
            if not src:
                return tag  # data:/상대경로/src 없음 → 유지
            url = src.group(2)
            norm = ("https:" + url) if url.startswith("//") else url
            host = (_up(norm).netloc or "").lower()
            if any(tok in host for tok in allowed_tokens):
                return tag  # our-domain/R2 → 유지
            return ""  # 외부(미러 실패) → 제거

        return _re.sub(r"<img\b[^>]*>", _keep, html, flags=_re.IGNORECASE)

    async def transform_single_image(
        self,
        product_id: str,
        image_url: str,
        mode: str = "video",
        model_preset: str = "female_v1",
    ) -> str | None:
        """단일 이미지를 AI 변환 후 URL 반환. 대표이미지를 건드리지 않는 독립 변환."""
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )

        repo = SambaCollectedProductRepository(self.session)
        product = await repo.get_async(product_id)
        if not product:
            return None

        preset = MODEL_PRESETS.get(model_preset, MODEL_PRESETS["female_v1"])
        model_desc = preset["desc"]

        category = " > ".join(
            filter(
                None,
                [
                    getattr(product, "category1", ""),
                    getattr(product, "category2", ""),
                    getattr(product, "category3", ""),
                ],
            )
        )

        try:
            img = await self._download_image(image_url)
            if mode == "background":
                # rembg: 무료, 로컬
                transformed = await self._remove_background_rembg(img)
            elif mode == "model":
                ref_image = await self._load_preset_image(model_preset)
                api_key, gm_model = await self._get_gemini_config()
                gemini_prompt = _get_category_prompt(category, mode, model_desc)
                transformed = await self._transform_image_gemini(
                    api_key, gm_model, img, gemini_prompt, ref_image
                )
            else:
                # 씬연출/비디오 등 모든 이미지 생성 → Gemini
                api_key, gm_model = await self._get_gemini_config()
                gemini_prompt = _get_category_prompt(category, mode, model_desc)
                ref_image = await self._load_preset_image(model_preset)
                transformed = await self._transform_image_gemini(
                    api_key, gm_model, img, gemini_prompt, ref_image
                )
            new_url = await self._save_image(transformed, image_url)
            return new_url
        except Exception as e:
            logger.error(f"[이미지] 단일 변환 실패 ({product_id}): {e}")
            return None

    async def transform_products(
        self,
        product_ids: list[str],
        scope: dict[str, bool],
        mode: str,
        model_preset: str = "female_v1",
    ) -> dict[str, Any]:
        """여러 상품의 이미지를 일괄 변환."""
        from backend.domain.samba.collector.repository import (
            SambaCollectedProductRepository,
        )

        repo = SambaCollectedProductRepository(self.session)

        # Gemini 키 로드 (배경제거 외 모든 이미지 생성)
        gemini_key: str | None = None
        gemini_model_name: str = ""
        if mode != "background":
            gemini_key, gemini_model_name = await self._get_gemini_config()

        is_auto = model_preset == "auto"

        # auto가 아니면 기존처럼 고정 프리셋
        if not is_auto:
            preset = MODEL_PRESETS.get(model_preset, MODEL_PRESETS["female_v1"])
            fixed_model_desc = preset["desc"]
            fixed_ref_image: bytes | None = None
            if mode == "model":
                fixed_ref_image = await self._load_preset_image(model_preset)

        results: list[dict[str, Any]] = []
        total_transformed = 0
        total_failed = 0

        # 이미지 다운로드용 공유 클라이언트 (연결 풀링으로 대량 처리 안정성 확보)
        _dl_client = httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

        for pid in product_ids:
            product = await repo.get_async(pid)
            if not product:
                results.append({"product_id": pid, "status": "not_found"})
                continue

            # 카테고리 조합
            category = " > ".join(
                filter(
                    None,
                    [
                        getattr(product, "category1", ""),
                        getattr(product, "category2", ""),
                        getattr(product, "category3", ""),
                    ],
                )
            )

            product_result: dict[str, Any] = {
                "product_id": pid,
                "transformed": 0,
                "failed": 0,
            }
            update_data: dict[str, Any] = {}
            product_images = product.images or []

            # auto 모드: 상품별 프리셋 자동 결정
            if is_auto and mode == "model":
                _, model_desc, ref_image = await self._resolve_preset_for_product(
                    product
                )
            elif not is_auto:
                model_desc = fixed_model_desc
                ref_image = fixed_ref_image
            else:
                model_desc = ""
                ref_image = None

            async def _transform_ai(img_bytes: bytes) -> bytes:
                """Gemini로 이미지 변환 (모델컷/씬연출/영상 모든 모드)."""
                if not gemini_key:
                    raise ValueError("Gemini 설정이 필요합니다")
                gemini_prompt = _get_category_prompt(category, mode, model_desc)
                return await self._transform_image_gemini(
                    gemini_key, gemini_model_name, img_bytes, gemini_prompt, ref_image
                )

            # ── 모델 착용 모드: 대표1장 + 추가3장 고정 생성 ──
            if mode == "model" and product_images:
                # 0) 대표이미지 다운로드 (디자인 기준으로 재사용)
                thumb_bytes: bytes | None = None
                try:
                    thumb_bytes = await self._download_image(
                        product_images[0], client=_dl_client
                    )
                except Exception as e:
                    logger.error(f"[이미지] {pid} 대표이미지 다운로드 실패: {e}")

                # 1) 대표이미지 변환
                new_thumb_url = None
                if thumb_bytes:
                    try:
                        transformed = await _transform_ai(thumb_bytes)
                        new_thumb_url = await self._save_image(
                            transformed, product_images[0]
                        )
                        product_result["transformed"] += 1
                    except Exception as e:
                        logger.error(f"[이미지] {pid} 대표이미지 변환 실패: {e}")
                        product_result["failed"] += 1

                # 2) 추가이미지 소스 결정: 추가이미지 있으면 사용, 없으면 상세이미지 참고
                raw_additional = (
                    list(product_images[1:]) if len(product_images) > 1 else []
                )

                # URL 패턴으로 비상품 이미지(배너/로고) 1차 필터링
                additional_sources = [
                    u for u in raw_additional if self._is_product_image(u)
                ]
                filtered_count = len(raw_additional) - len(additional_sources)
                if filtered_count > 0:
                    logger.info(
                        f"[이미지] {pid} 추가이미지 {filtered_count}장 필터링 (배너/로고 제외)"
                    )

                used_detail_as_ref = False
                if not additional_sources:
                    # 상세이미지에서 참고용 소스 가져오기 (상세이미지 자체는 변경 안함)
                    raw_detail = list(product.detail_images or [])
                    additional_sources = [
                        u for u in raw_detail if self._is_product_image(u)
                    ]
                    used_detail_as_ref = True
                    if additional_sources:
                        logger.info(
                            f"[이미지] {pid} 추가이미지 없음 → 상세이미지 {len(additional_sources)}장 참고"
                        )

                # 3) 소스에서 최대 2개 뽑아 변환 → 추가이미지 2장 생성
                #    다운로드 후 이미지 비율도 2차 검증하여 배너 제거
                new_additional: list[str] = []
                src_idx = 0
                attempts = 0
                max_attempts = (
                    max(len(additional_sources) * 2, 6) if additional_sources else 3
                )
                while len(new_additional) < 2 and attempts < max_attempts:
                    if additional_sources:
                        src_url = additional_sources[src_idx % len(additional_sources)]
                        src_idx += 1
                    else:
                        src_url = product_images[0]
                    attempts += 1
                    try:
                        img = await self._download_image(src_url, client=_dl_client)
                        # 다운로드 후 이미지 비율 2차 검증
                        if additional_sources and not self._is_product_image(
                            src_url, img
                        ):
                            logger.info(
                                f"[이미지] {pid} 비상품 이미지 스킵 (비율 이상): {src_url[:80]}"
                            )
                            continue
                        transformed = await _transform_ai(img)
                        new_url = await self._save_image(transformed, src_url)
                        new_additional.append(new_url)
                        product_result["transformed"] += 1
                    except Exception as e:
                        logger.error(
                            f"[이미지] {pid} 추가이미지({len(new_additional) + 1}/3) 변환 실패: {e}"
                        )
                        product_result["failed"] += 1
                        # 소스 없으면 더 이상 시도 불필요
                        if not additional_sources:
                            break

                # 4) 최종: 대표1장 + 추가3장만 남김 (나머지 삭제)
                final_images = [new_thumb_url or product_images[0]] + new_additional
                update_data["images"] = final_images
                # 상세이미지는 그대로 유지 (변경 안함)

            # ── 연출컷 모드 ──
            elif mode == "scene" and product_images:
                has_additional = len(product_images) > 1
                # 대표이미지 변환
                try:
                    img = await self._download_image(
                        product_images[0], client=_dl_client
                    )
                    transformed = await _transform_ai(img)
                    new_url = await self._save_image(transformed, product_images[0])
                    updated_images = list(product_images)
                    updated_images[0] = new_url
                    update_data["images"] = updated_images
                    product_result["transformed"] += 1
                except Exception as e:
                    logger.error(f"[이미지] {pid} 대표이미지 변환 실패: {e}")
                    product_result["failed"] += 1

                # 추가이미지 변환
                if has_additional:
                    base_images = list(update_data.get("images", product_images))
                    for idx in range(1, len(base_images)):
                        try:
                            img = await self._download_image(
                                base_images[idx], client=_dl_client
                            )
                            transformed = await _transform_ai(img)
                            new_url = await self._save_image(
                                transformed, base_images[idx]
                            )
                            base_images[idx] = new_url
                            product_result["transformed"] += 1
                        except Exception as e:
                            logger.error(f"[이미지] {pid} 추가이미지 변환 실패: {e}")
                            product_result["failed"] += 1
                    update_data["images"] = base_images
                elif product.detail_images:
                    # 추가이미지 없으면 상세이미지 변환
                    new_details = []
                    for img_url in product.detail_images or []:
                        try:
                            img = await self._download_image(img_url, client=_dl_client)
                            transformed = await _transform_ai(img)
                            new_url = await self._save_image(transformed, img_url)
                            new_details.append(new_url)
                            product_result["transformed"] += 1
                        except Exception as e:
                            logger.error(f"[이미지] {pid} 상세이미지 변환 실패: {e}")
                            new_details.append(img_url)
                            product_result["failed"] += 1
                    update_data["detail_images"] = new_details

            # ── 모델→상품 모드: Gemini로 모델 제거 → 상품컷 생성 ──
            elif mode == "model_to_product" and product_images:
                if not gemini_key:
                    product_result["failed"] += 1
                    logger.error(f"[모델→상품] {pid} Gemini 설정 필요")
                else:
                    m2p_prompt = _get_category_prompt(category, "model_to_product", "")
                    updated_images = list(product_images)
                    for idx, img_url in enumerate(updated_images):
                        try:
                            img = await self._download_image(img_url, client=_dl_client)
                            transformed = await self._transform_image_gemini(
                                gemini_key, gemini_model_name, img, m2p_prompt, None
                            )
                            new_url = await self._save_image(transformed, img_url)
                            updated_images[idx] = new_url
                            product_result["transformed"] += 1
                        except Exception as e:
                            logger.error(f"[모델→상품] {pid} 이미지 변환 실패: {e}")
                            product_result["failed"] += 1

                    if product_result["transformed"] > 0:
                        update_data["images"] = updated_images

            # ── 배경제거 등 기본 모드: scope 그대로 사용 ──
            else:
                use_thumbnail = scope.get("thumbnail", False)
                use_additional = scope.get("additional", False)
                use_detail = scope.get("detail", False)
                is_bg_mode = mode == "background"

                async def _transform(img_bytes: bytes) -> bytes:
                    """배경제거 → rembg, 그 외 → Gemini."""
                    if is_bg_mode:
                        return await self._remove_background_rembg(img_bytes)
                    if gemini_key:
                        gemini_prompt = _get_category_prompt(category, mode, model_desc)
                        return await self._transform_image_gemini(
                            gemini_key,
                            gemini_model_name,
                            img_bytes,
                            gemini_prompt,
                            ref_image,
                        )
                    return await self._remove_background_rembg(img_bytes)

                # 대표이미지 변환
                if use_thumbnail and product_images:
                    try:
                        img = await self._download_image(
                            product_images[0], client=_dl_client
                        )
                        transformed = await _transform(img)
                        new_url = await self._save_image(transformed, product_images[0])
                        updated_images = list(product_images)
                        updated_images[0] = new_url
                        update_data["images"] = updated_images
                        product_result["transformed"] += 1
                    except Exception as e:
                        logger.error(f"[이미지] {pid} 대표이미지 변환 실패: {e}")
                        product_result["failed"] += 1

                # 추가이미지 변환
                if use_additional and product_images and len(product_images) > 1:
                    base_images = list(update_data.get("images", product_images))
                    for idx in range(1, len(base_images)):
                        try:
                            img = await self._download_image(
                                base_images[idx], client=_dl_client
                            )
                            transformed = await _transform(img)
                            new_url = await self._save_image(
                                transformed, base_images[idx]
                            )
                            base_images[idx] = new_url
                            product_result["transformed"] += 1
                        except Exception as e:
                            logger.error(f"[이미지] {pid} 추가이미지 변환 실패: {e}")
                            product_result["failed"] += 1
                    update_data["images"] = base_images

                # 상세이미지 변환
                if use_detail and product.detail_images:
                    new_details = []
                    for img_url in product.detail_images or []:
                        try:
                            img = await self._download_image(img_url, client=_dl_client)
                            transformed = await _transform(img)
                            new_url = await self._save_image(transformed, img_url)
                            new_details.append(new_url)
                            product_result["transformed"] += 1
                        except Exception as e:
                            logger.error(f"[이미지] {pid} 상세이미지 변환 실패: {e}")
                            new_details.append(img_url)
                            product_result["failed"] += 1
                    update_data["detail_images"] = new_details

            # DB 업데이트 — __ai_image__ + __img_edited__ 태그 추가 + 컬럼 SET
            if update_data and product_result["transformed"] > 0:
                existing_tags = list(product.tags or [])
                if "__ai_image__" not in existing_tags:
                    existing_tags.append("__ai_image__")
                if "__img_edited__" not in existing_tags:
                    existing_tags.append("__img_edited__")
                update_data["tags"] = existing_tags
                update_data["ai_image_transformed"] = True
            if update_data:
                try:
                    await repo.update_async(pid, **update_data)
                except Exception as e:
                    logger.error(f"[이미지] {pid} DB 업데이트 실패: {e}")

            total_transformed += product_result["transformed"]
            total_failed += product_result["failed"]
            results.append(product_result)

        # 예외 발생 시에도 클라이언트 정리 보장
        try:
            await _dl_client.aclose()
        except Exception:
            pass
        await self.session.commit()
        return {
            "message": f"변환 완료 — 성공 {total_transformed}건, 실패 {total_failed}건",
            "total_transformed": total_transformed,
            "total_failed": total_failed,
            "details": results,
        }

    async def sync_presets_to_r2(self) -> dict[str, Any]:
        """로컬 프리셋 이미지를 R2에 일괄 업로드."""
        import asyncio as _aio
        from functools import partial

        r2 = await self._get_r2_client()
        if not r2:
            return {"success": False, "message": "R2 설정이 없습니다."}

        client, bucket_name, _ = r2
        uploaded: list[str] = []
        failed: list[dict[str, str]] = []

        for key, preset in MODEL_PRESETS.items():
            filename = preset.get("image", "")
            if not filename:
                continue
            local_path = PRESET_IMAGE_DIR / filename
            if not local_path.exists():
                failed.append({"key": key, "reason": "로컬 파일 없음"})
                continue
            try:
                await _aio.to_thread(
                    partial(
                        client.upload_fileobj,
                        io.BytesIO(local_path.read_bytes()),
                        bucket_name,
                        f"model_presets/{filename}",
                        ExtraArgs={"ContentType": "image/png"},
                    ),
                )
                uploaded.append(key)
            except Exception as e:
                failed.append({"key": key, "reason": str(e)[:100]})

        return {
            "success": True,
            "message": f"R2 업로드 완료 — 성공 {len(uploaded)}건, 실패 {len(failed)}건",
            "uploaded": uploaded,
            "failed": failed,
        }


# ── 긴 이미지 분할 (수집 시 상세이미지→추가이미지 보충용) ─────────


async def split_long_images(
    image_urls: list[str],
    original_count: int,
    session: AsyncSession,
) -> list[str]:
    """추가이미지에 보충된 상세이미지 중 긴 이미지를 분할.

    Args:
      image_urls: 전체 이미지 URL 리스트 (썸네일+추가+보충된 상세)
      original_count: 보충 전 원본 이미지 수 (이 인덱스 이후가 상세이미지)
      session: DB 세션 (R2 설정 조회용)

    Returns:
      분할 처리된 이미지 URL 리스트 (최대 9장)
    """
    if original_count >= len(image_urls):
        return image_urls  # 보충된 이미지 없음

    # R2 클라이언트 준비
    from backend.domain.samba.forbidden.repository import SambaSettingsRepository

    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key="cloudflare_r2")
    if not row or not isinstance(row.value, dict):
        return image_urls
    creds = row.value
    account_id = str(creds.get("accountId", "")).strip()
    access_key = str(creds.get("accessKey", "")).strip()
    secret_key = str(creds.get("secretKey", "")).strip()
    bucket_name = str(creds.get("bucketName", "")).strip()
    public_url = str(creds.get("publicUrl", "")).strip().rstrip("/")
    if not access_key or not secret_key or not bucket_name:
        return image_urls

    import boto3

    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    # 원본 이미지는 그대로 유지
    result = list(image_urls[:original_count])

    # 보충된 상세이미지만 처리
    for url in image_urls[original_count:]:
        if len(result) >= 9:
            break
        try:
            split_urls = await _split_single_image(url, r2, bucket_name, public_url)
            for su in split_urls:
                if len(result) < 9:
                    result.append(su)
        except Exception as e:
            logger.warning(f"[이미지분할] 실패 {url}: {e}")
            if len(result) < 9:
                result.append(url)

    return result


async def _split_single_image(
    url: str,
    r2_client: Any,
    bucket_name: str,
    public_url: str,
) -> list[str]:
    """단일 이미지 다운로드 → 비율 체크 → 분할 → R2 업로드."""
    from PIL import Image
    from urllib.parse import urlparse

    parsed = urlparse(url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"
    if "msscdn.net" in (parsed.netloc or ""):
        referer = "https://www.musinsa.com/"

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": referer,
            },
        )
        resp.raise_for_status()

    img = Image.open(io.BytesIO(resp.content))
    w, h = img.size

    # 텍스트 이미지면 제외
    if _is_text_image(img):
        logger.info(f"[이미지분할] 텍스트 이미지 제외: {url} ({w}x{h})")
        return []

    # 세로가 가로의 2배 이하 → 분할 불필요
    if h <= w * 2:
        return [url]

    # 상단 텍스트 영역 감지 → 텍스트 끝나는 지점부터 분할 시작
    crop_y = _find_content_start(img, w, h)
    if crop_y > 0:
        logger.info(f"[이미지분할] 상단 텍스트 {crop_y}px 제거: {url}")

    # 가로 크기 기준 정사각형 단위로 분할
    segment_h = w
    segments: list[Image.Image] = []
    y = crop_y
    while y < h:
        bottom = min(y + segment_h, h)
        # 마지막 세그먼트가 가로의 1/3 미만이면 버림
        if bottom - y < w // 3 and segments:
            break
        segments.append(img.crop((0, y, w, bottom)))
        y = bottom

    # 텍스트 이미지 필터링 후 R2에 업로드
    # [2026-05-24] content_hash 기반 결정적 키 + HeadObject 가드로 중복 PUT 차단
    # 기존: uuid.uuid4() 가 키에 박혀 같은 segment 매번 새 객체 PUT → GCP egress 누수 주범
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    uploaded: list[str] = []
    for idx, seg in enumerate(segments):
        if _is_text_image(seg):
            logger.info(f"[이미지분할] 텍스트 이미지 제외: {url} seg#{idx}")
            continue
        buf = io.BytesIO()
        # 마켓 API 호환을 위해 JPEG로 저장 (WebP는 11번가 등에서 거부됨)
        seg.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
        seg_bytes = buf.getvalue()
        content_hash = hashlib.md5(seg_bytes).hexdigest()[:16]
        filename = f"split_{url_hash}_{idx}_{content_hash}.jpg"
        key = f"split/{filename}"

        def _exists(_key: str = key) -> bool:
            try:
                r2_client.head_object(Bucket=bucket_name, Key=_key)
                return True
            except Exception:
                return False

        if not await asyncio.to_thread(_exists):
            buf.seek(0)
            await asyncio.to_thread(
                partial(
                    r2_client.upload_fileobj,
                    buf,
                    bucket_name,
                    key,
                    ExtraArgs={"ContentType": "image/jpeg"},
                ),
            )
        uploaded.append(f"{public_url}/{key}")

    logger.info(f"[이미지분할] {url} → {len(uploaded)}장 ({w}x{h})")
    return uploaded


def _find_content_start(img: Any, w: int, h: int) -> int:
    """상단 텍스트/공지 영역의 끝(상품 사진 시작) y좌표를 반환.

    위에서부터 가로 스트립(높이 = 가로의 1/10)을 스캔하여
    컬러 픽셀이 10% 이상인 첫 스트립의 시작 y를 반환한다.
    """
    from PIL import Image

    strip_h = max(w // 10, 20)
    # 성능을 위해 가로를 200px로 리사이즈
    scale = min(1.0, 200 / w)
    sw = int(w * scale)

    y = 0
    while y < h:
        bottom = min(y + strip_h, h)
        strip = img.crop((0, y, w, bottom))
        if sw < w:
            strip = strip.resize((sw, int((bottom - y) * scale)), Image.LANCZOS)
        hsv = strip.convert("HSV")
        hist = hsv.split()[1].histogram()
        total = sum(hist)
        color_pixels = sum(hist[30:])
        if total > 0 and color_pixels / total >= 0.10:
            return y  # 상품 사진 시작 지점
        y = bottom

    return 0  # 전체가 텍스트면 처음부터 분할


def _is_text_image(img: Any) -> bool:
    """텍스트/공지 이미지 판별 — 컬러 픽셀 비율이 10% 미만이면 텍스트로 간주.

    상품 사진은 색상이 다양하지만, 텍스트 이미지(공지사항, 배송안내 등)는
    흰 배경 + 검은 글씨로 채도가 거의 없다.
    """
    from PIL import Image

    rgb = img.convert("RGB")
    # 성능을 위해 리사이즈 (최대 200px)
    w, h = rgb.size
    if max(w, h) > 200:
        ratio = 200 / max(w, h)
        rgb = rgb.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

    hsv = rgb.convert("HSV")
    # S(채도) 채널 히스토그램 — 0~255, 인덱스 0~255
    hist = hsv.split()[1].histogram()  # S 채널
    total_pixels = sum(hist)
    # 채도 30 이상인 픽셀 수 = 컬러 픽셀
    color_pixels = sum(hist[30:])
    color_ratio = color_pixels / total_pixels if total_pixels > 0 else 0
    return color_ratio < 0.10
