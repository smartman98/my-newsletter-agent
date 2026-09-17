# -*- coding: utf-8 -*-
"""④ 검증 — 답변에 근거 없는 내용이 섞였는지 기계적으로 검사한다.

두 방향을 같이 본다.
- 정방향: 답변에 나온 숫자·시각·기한이 근거 문서나 조회 결과에 실제로 있는가(지어냈는가).
- 역방향: 개인 기록·최종 판정을 단정하는 표현을 썼는가(문서가 금지한 말).

앞선 과제(모두몰 에이전트)에서 배운 것: 지어낸 숫자를 잡는 장치만 있으면 "빠뜨린 것"과
"단정한 것"은 그대로 새어 나간다. 그래서 금지 표현 검사를 같이 넣었다.
"""
import json
import re

from context import build_context

# 문서에 없는데 그럴듯하게 나오기 쉬운 단정 표현. 문서 7.3이 "하지 말아야 할 것"으로 규정한다.
FORBIDDEN_PATTERNS = [
    (r"제적(됩니다|될 것|되실|되지 않습니다|안 됩니다)", "개인의 제적 여부를 단정함"),
    (r"수료(하실 수 있습니다|가 확정|됩니다)(?!\s*기준)", "개인의 수료 여부를 단정함"),
    (r"공가(가|로)?\s*(인정됩니다|인정되지 않습니다|처리됩니다)", "공가 인정 여부를 단정함"),
    (r"장려금(은|이)?\s*\d+\s*(만)?원", "훈련장려금 지급액을 임의로 안내함"),
    (r"(결석|지각)\s*(횟수는|기록은)\s*\d+\s*(회|번)", "개인의 출결 기록을 단정함"),
]

_NUM_RE = re.compile(r"\d[\d,]*")


def _norm(s: str) -> str:
    return s.replace(",", "")


def _numbers_in(text: str) -> set[str]:
    return {_norm(m) for m in _NUM_RE.findall(text or "")}


def allowed_numbers(category: str, tool_results: dict | None) -> set[str]:
    """근거로 쓸 수 있는 숫자 = 그 카테고리에 들어간 문서 조각 + 조회 결과."""
    allowed = _numbers_in(build_context(category))
    if tool_results:
        allowed |= _numbers_in(json.dumps(tool_results, ensure_ascii=False))
    return allowed


def verify_answer(answer: str, category: str, tool_results: dict | None = None) -> dict:
    """답변을 검증한다. 반환: {"ok": bool, "violations": [...], "checked": {...}}"""
    violations = []

    allowed = allowed_numbers(category, tool_results)
    found = _numbers_in(answer)
    # 한 자리 숫자는 문장 구조("1~3문장", "세 가지" 등)에 섞여 오탐이 많아 두 자리 이상만 본다.
    suspicious = sorted(n for n in found if len(n) >= 2 and n not in allowed)
    if suspicious:
        violations.append({
            "type": "근거 없는 수치",
            "detail": f"근거 문서·조회 결과에 없는 숫자가 답변에 있음: {suspicious}",
        })

    for pattern, label in FORBIDDEN_PATTERNS:
        m = re.search(pattern, answer)
        if m:
            violations.append({"type": "금지된 단정", "detail": f"{label} — \"{m.group(0)}\""})

    return {
        "ok": not violations,
        "violations": violations,
        "checked": {
            "numbers_in_answer": sorted(found),
            "suspicious": suspicious,
            "category": category,
        },
    }
