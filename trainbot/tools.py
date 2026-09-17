# -*- coding: utf-8 -*-
"""조회 도구. 근거 문서에 값이 그대로 적혀 있지 않고 **건마다 달라지는 것**만 도구로 만든다.

문서에 그대로 적힌 값(지각 10시 11분, 수료 80%, 서류 4요건)은 도구가 필요 없다 — 프롬프트의
근거 문서에 이미 들어 있으므로 바로 답하면 된다. 반대로 아래 셋은 기억으로 답하면 반드시
틀린다.

- **공가 사유별 인정일·증빙자료** — 사유가 9종인데 인정 일수도 서류도 전부 다르다.
- **장려금 금액** — 1일 20,000원이지만 최대 20일 상한이 걸려 단순 곱셈이 아니다.
- **기한 계산** — 발생일 +1일, 일주일 이내처럼 날짜를 더해야 한다.
"""
from datetime import date, datetime, timedelta

# ---------- 문서 1.2 단위기간 ----------
UNIT_PERIODS = [
    (1, date(2026, 7, 8), date(2026, 7, 31), 17),
    (2, date(2026, 8, 10), date(2026, 9, 7), 20),
    (3, date(2026, 9, 8), date(2026, 10, 7), 19),
    (4, date(2026, 10, 8), date(2026, 11, 6), 21),
    (5, date(2026, 11, 9), date(2026, 12, 7), 21),
    (6, date(2026, 12, 8), date(2027, 1, 7), 21),
    (7, date(2027, 1, 8), date(2027, 1, 8), 1),
]

TOTAL_DAYS = 120

# ---------- 문서 3.2 공가 사유별 기준 (공식 「공가 리스트」) ----------
LEAVE_REASONS = {
    "예비군/민방위": {
        "인정일": "소요일", "증빙자료": ["예비군 훈련 증명서"], "비고": [],
    },
    "질병/입원": {
        "인정일": "단위 기간 내 2일 (최대 11일)",
        "증빙자료": ["진료확인서", "통원확인서", "처방전", "진단서", "입/퇴원확인서"],
        "증빙자료_비고": "위 중 택 1, 질병분류기호 기재 필수",
        "비고": [
            "본인 및 자녀(만 19세 미만) 진료 시에만 가능",
            "자녀 병가 시 가족관계증명서 추가 제출",
            "영수증 제출 시 처리 불가",
            "지각/조퇴/외출 시에도 공결 인정",
            "사용 시 단위 기간 내 누적 횟수 확인 필요",
        ],
    },
    "사망": {
        "인정일": "대상에 따라 다름 — 배우자·본인 및 배우자의 부모 5일 / 조부모 3일 / 형제자매 1일",
        "증빙자료": ["사망진단서", "가족관계증명서"],
        "비고": ["사망일 기준으로 소요일 적용"],
    },
    "휴가": {
        "인정일": "단위 기간 내 1일 (최대 6일)",
        "증빙자료": ["휴가신청서(지정 양식)"],
        "비고": [
            "당일 또는 발생일 이후 사용 불가 (부득이한 사유 제외)",
            "휴가 사용 전 신청서 제출 필수",
            "다음달 휴가를 당겨쓰기 불가",
        ],
    },
    "입사시험": {
        "인정일": "소요일",
        "증빙자료": ["면접 확인서", "채용공고 캡처 이미지"],
        "비고": [],
    },
    "자격시험": {
        "인정일": "소요일",
        "증빙자료": ["응시확인서"],
        "비고": [
            "훈련 과정과 관련된 자격 시험만 가능",
            "응시확인서 발급이 늦으면 수험표에 시험 당일 감독관 확인 도장·서명을 받아 제출",
        ],
    },
    "결혼": {
        "인정일": "본인 5일, 자녀 1일",
        "증빙자료": ["청첩장"],
        "비고": ["결혼일 기준으로 소요일 적용"],
    },
    "출산(배우자)": {
        "인정일": "5일",
        "증빙자료": ["출산확인서"],
        "비고": ["출산일 기준으로 소요일 적용"],
    },
    "해커톤 참여": {
        "인정일": "소요일",
        "증빙자료": ["참여확인서"],
        "비고": ["정부에서 주관·주최하는 훈련 과정 관련 경진대회만 출석 인정 (KDT 해커톤 인정)"],
    },
}

# 훈련생이 실제로 쓰는 표현 → 공식 사유명
_ALIASES = {
    "예비군": "예비군/민방위", "민방위": "예비군/민방위",
    "병원": "질병/입원", "아파": "질병/입원", "질병": "질병/입원", "입원": "질병/입원",
    "진료": "질병/입원", "몸살": "질병/입원", "감기": "질병/입원",
    "장례": "사망", "상": "사망", "조부모": "사망", "부고": "사망",
    "연차": "휴가", "월차": "휴가", "쉬고": "휴가",
    "면접": "입사시험", "채용": "입사시험", "입사": "입사시험",
    "자격증": "자격시험", "시험": "자격시험",
    "결혼식": "결혼", "웨딩": "결혼",
    "출산": "출산(배우자)",
    "해커톤": "해커톤 참여", "경진대회": "해커톤 참여",
}


def _parse(d: str | None) -> date:
    if not d:
        return date.today()
    return datetime.strptime(d.strip(), "%Y-%m-%d").date()


def lookup_leave_reason(reason: str) -> dict:
    """공가 사유별 인정 일수·증빙자료·주의사항을 조회한다.

    사유마다 인정 일수와 필요한 서류가 전부 다르므로, 공가 문의는 기억으로 답하지 말고
    반드시 이 도구로 확인한다. 훈련생이 쓴 표현("병원 다녀왔어요")으로도 찾을 수 있다.
    """
    q = (reason or "").strip()
    if q in LEAVE_REASONS:
        return {"reason": q, **LEAVE_REASONS[q]}
    for alias, official in _ALIASES.items():
        if alias in q:
            return {"reason": official, "matched_from": q, **LEAVE_REASONS[official]}
    for official in LEAVE_REASONS:
        if official.split("/")[0] in q:
            return {"reason": official, "matched_from": q, **LEAVE_REASONS[official]}
    return {
        "error": "공가 리스트에 없는 사유입니다",
        "input": reason,
        "available": sorted(LEAVE_REASONS),
        "note": "목록에 없는 사유는 인정 여부를 단정하지 말고 운영 매니저에게 확인하도록 안내하십시오.",
    }


def calculate_deadline(event_date: str | None = None) -> dict:
    """공가 신청·증빙서류 제출 기한을 계산한다(문서 3.1: 신청은 발생일 +1일, 서류는 일주일 이내)."""
    try:
        d = _parse(event_date)
    except ValueError:
        return {"error": "날짜 형식은 YYYY-MM-DD 입니다", "input": event_date}
    return {
        "event_date": d.isoformat(),
        "application_deadline": (d + timedelta(days=1)).isoformat(),
        "application_rule": "공가 신청은 발생일 +1일까지",
        "document_deadline": (d + timedelta(days=7)).isoformat(),
        "document_rule": "증빙 서류는 일주일 이내 제출",
        "required_fields": ["이름", "생년월일", "일자", "발급기관 직인"],
    }


DAILY_ALLOWANCE = 20000   # 기본 10,000원 + KDT 특별수당 10,000원
ALLOWANCE_CAP_DAYS = 20   # 20일 이상 출석해도 20일로 책정


def calculate_allowance(attended_days: int) -> dict:
    """출석 일수로 훈련장려금을 계산한다(문서 4.1: 1일 20,000원, 최대 20일 기준).

    20일 상한이 걸려 단순 곱셈이 아니므로 반드시 이 도구로 계산한다.
    """
    try:
        days = int(attended_days)
    except (TypeError, ValueError):
        return {"error": "출석 일수는 숫자여야 합니다", "input": attended_days}
    if days < 0:
        return {"error": "출석 일수는 0 이상이어야 합니다", "input": attended_days}
    paid_days = min(days, ALLOWANCE_CAP_DAYS)
    return {
        "attended_days": days,
        "paid_days": paid_days,
        "daily_amount": DAILY_ALLOWANCE,
        "total_amount": paid_days * DAILY_ALLOWANCE,
        "cap_applied": days > ALLOWANCE_CAP_DAYS,
        "rule": "1일 20,000원(기본 10,000원 + KDT 특별수당 10,000원) × 출석 일수, 최대 20일 기준",
        "note": "실제 지급 여부는 단위기간 80% 이상 출석과 고용형태에 따라 결정되며 운영진이 확인합니다.",
    }


def lookup_schedule(target_date: str | None = None) -> dict:
    """해당 날짜가 몇 번째 단위기간인지와 그 기간의 훈련일수를 찾는다."""
    try:
        d = _parse(target_date)
    except ValueError:
        return {"error": "날짜 형식은 YYYY-MM-DD 입니다", "input": target_date}
    for no, start, end, days in UNIT_PERIODS:
        if start <= d <= end:
            return {"date": d.isoformat(), "unit_period": no,
                    "period_start": start.isoformat(), "period_end": end.isoformat(),
                    "period_days": days, "course_total_days": TOTAL_DAYS}
    return {"date": d.isoformat(), "unit_period": None,
            "note": "훈련 기간(2026-07-08 ~ 2027-01-08) 밖이거나 단위기간 사이의 휴식 구간입니다.",
            "course_total_days": TOTAL_DAYS}


def escalate_to_staff(reason: str, question: str | None = None) -> dict:
    """운영진에게 넘긴다. 개인 기록 조회·최종 판정처럼 문서로 답할 수 없을 때 호출한다."""
    return {"escalated": True, "reason": reason, "question": question,
            "message": "정확한 확인을 위해 운영 매니저에게 문의해 주세요."}


TOOLS = {f.__name__: f for f in [lookup_leave_reason, calculate_deadline,
                                 calculate_allowance, lookup_schedule, escalate_to_staff]}
