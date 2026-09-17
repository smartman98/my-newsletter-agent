# -*- coding: utf-8 -*-
"""한곳에 모아 둔 설정.

모델은 용도별로 따로 둔다 — 앞선 과제(모두몰 에이전트)에서 실측해 보니 분류와 답변에서
가장 좋은 모델이 서로 달랐다. 분류는 gpt-4o가, 도구를 쓰는 답변 생성은 gpt-6-astra가 좋았다.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # OS에 남아 있는 옛 키보다 .env를 우선한다

BASE = Path(__file__).parent

MODEL = os.environ.get("ROUTE_MODEL", "gpt-4o")              # 분류용
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "gpt-6-astra")  # 답변 생성용

CONF_THRESHOLD = 0.5   # 이 값 미만이면 운영진에게 넘긴다
MAX_TOOL_TURNS = 3     # 도구 호출 루프 상한
WORKERS = 8            # 평가 시 동시 호출 수

CATEGORIES = ["ATTENDANCE", "LEAVE", "ALLOWANCE", "CONDUCT", "PROGRAM", "OTHER"]
ANSWERABLE = ["ATTENDANCE", "LEAVE", "ALLOWANCE", "CONDUCT", "PROGRAM"]  # OTHER 제외


def chat_kwargs(model: str, for_tools: bool = False) -> dict:
    """모델마다 지원하는 옵션이 달라 여기서 맞춘다(모두몰 과제에서 실측한 내용).

    - gpt-6 세대·o 시리즈는 temperature를 바꾸는 것 자체를 거부한다(기본값만 허용).
    - gpt-5 세대는 추론 모드와 도구 호출을 함께 못 써서 reasoning_effort="none"이 필요하다.
    - gpt-6 세대는 chat/completions에서 도구 호출이 아예 안 되고 Responses API를 써야 한다.
    """
    kw = {"timeout": 60, "max_retries": 2}
    if not model.startswith(("gpt-6", "o1", "o3", "o4")):
        kw["temperature"] = 0
    if for_tools and model.startswith("gpt-5"):
        kw["reasoning_effort"] = "none"
    if for_tools and model.startswith("gpt-6"):
        kw["use_responses_api"] = True
    return kw
