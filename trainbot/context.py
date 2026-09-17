# -*- coding: utf-8 -*-
"""근거 문서를 장 단위로 쪼개고, 카테고리에 필요한 장만 골라 프롬프트용 근거를 조립한다.

전문을 통째로 넣지 않는 이유는 두 가지다.
1. 관련 없는 규정이 섞이면 모델이 엉뚱한 근거로 답한다. 예를 들어 공가 문의 프롬프트에
   제적 기준이 같이 들어가 있으면 "서류 미제출 시 제적됩니다" 같은 과장된 답이 나온다.
2. 입력이 길어져 비용·지연이 커진다.
"""
import re
from pathlib import Path

BASE = Path(__file__).parent
DOC = BASE / "docs" / "training_rules.md"


def split_sections(text: str) -> dict:
    """'## ' 헤딩 단위로 문서를 쪼갠다. 키는 장 번호(문자열)."""
    parts = re.split(r"^## ", text, flags=re.M)
    out = {"_header": parts[0].strip()}
    for p in parts[1:]:
        title = p.split("\n", 1)[0].strip()
        m = re.match(r"(\d+)\.", title)
        key = m.group(1) if m else title
        out[key] = "## " + p.rstrip()
    return out


sections = split_sections(DOC.read_text(encoding="utf-8"))


# 카테고리 → 근거 문서 매핑표.
# "왜 이렇게 나눴는가"는 문서의 장 구성이 곧 담당 업무의 구분이기 때문이다. 출결(2장)은
# 시각·기준 안내, 공가(3장)는 신청 절차, 수료·장려금(4장)은 자격 요건, 제적·참여규칙(5·6장)은
# 징계 기준으로, 답하는 내용의 성격이 서로 다르다.
SECTION_MAP = {
    "ATTENDANCE": ["2"],        # 출결 기준 — 지각·조퇴·외출·결석
    "LEAVE":      ["3"],        # 공가·휴가 — 신청 기한·서류·사유별 기준
    "ALLOWANCE":  ["4"],        # 훈련장려금 + 고용형태
    "CONDUCT":    ["5"],        # 제적·경고 기준 + 참여 규칙
    "PROGRAM":    ["1"],        # 과정 정보·수료/졸업·도메인 개발하기
    "OTHER":      [],           # 범위 밖 — 붙일 근거가 없다는 것 자체가 판정이다
}

# 어느 문의든 항상 들어가는 장. 응대 원칙(0), 과정 기본 정보(1), 범위 밖·이관(7)은
# 카테고리와 무관하게 지켜야 하는 규칙이라 항상 포함한다. (1장 과정 정보는 PROGRAM
# 전용으로 옮겼다 — 모든 답변에 단위기간 표가 붙을 이유가 없다.)
ALWAYS = ["_header", "0", "7"]

CATEGORY_LABELS = {
    "ATTENDANCE": "출결",
    "LEAVE": "공가·휴가",
    "ALLOWANCE": "훈련장려금·고용형태",
    "CONDUCT": "제적·경고·참여규칙",
    "PROGRAM": "과정 운영·수료·도메인 개발",
    "OTHER": "응대 범위 밖",
}


def build_context(category: str, secs: dict | None = None) -> str:
    """카테고리에 필요한 근거만 이어 붙인다."""
    secs = sections if secs is None else secs
    keys = [k for k in ALWAYS + SECTION_MAP.get(category, []) if k in secs]
    return "\n\n".join(secs[k] for k in keys)


def used_sections(category: str) -> list[str]:
    """데모 화면에서 '어느 근거를 썼는지' 보여주기 위한 목록."""
    return [k for k in ALWAYS + SECTION_MAP.get(category, []) if k != "_header"]


def build_answer_prompt(question: str, category: str, tool_results: dict | None = None) -> str:
    """답변 생성용 시스템 프롬프트를 조립한다."""
    import json

    from prompts import ANSWER_RULES

    ctx = build_context(category)
    tr = json.dumps(tool_results or {}, ensure_ascii=False, indent=1)
    return (f"{ANSWER_RULES}\n"
            f"===== 근거 문서 (카테고리: {category} · {CATEGORY_LABELS.get(category, category)}) =====\n"
            f"{ctx}\n\n"
            f"===== 조회 결과 =====\n{tr}\n")
