"""피어리뷰용 — 리뷰어가 아이디/비밀번호 없이 링크만 눌러서 파이프라인을 실제로
돌려볼 수 있게 하는 최소한의 웹 서버(2026-09-15 사용자 요청).

로그인 화면은 없지만, 아무나 눌러도 실제 Claude 호출 비용이 나가지 않도록
/run에는 URL에 붙이는 짧은 토큰(REVIEW_TOKEN) 하나만 요구한다 — "로그인"이
아니라 그냥 공유 링크에 붙는 열쇠다.

/run(GET)은 바로 실행하지 않고 "실행하기" 버튼이 있는 화면을 보여준다(2026-09-16
사용자 요청 — 링크 열자마자 자동으로 실행되는 게 아니라, 버튼을 눌러야 시작되게).
버튼을 누르면(POST) 파이프라인을 백그라운드로 시작만 하고 바로 결과 확인
페이지로 넘긴다(원래는 끝날 때까지 응답을 붙잡고 있었는데, Render 무료 플랜이
1분 안팎에서 연결을 끊어버려 "헛바퀴"처럼 보이는 502가 발생했음 — 2026-09-15
확인). 결과 확인 페이지는 완료되면 최종 선택된 기사의 제목·요약·인사이트를
그대로 보여준다.
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from graph import build

# 훈련생 문의 응대 봇(my-routing-agent)을 같은 서버에 얹는다(2026-09-17 사용자 요청).
# 그쪽 모듈들은 서로를 최상위 이름(`from config import ...`)으로 부르도록 짜여 있어서,
# 패키지로 감싸지 않고 폴더 자체를 import 경로에 추가한다.
sys.path.insert(0, str(Path(__file__).parent / "trainbot"))

app = FastAPI(title="증시 뉴스 다이제스트 — 피어리뷰용 실행기")

JOBS: dict[str, dict] = {}

PAGE_HEAD = "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
BODY_STYLE = "font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.6"


def _check_token(token: str) -> None:
    expected = os.environ.get("REVIEW_TOKEN")
    if not expected or token != expected:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")


@app.get("/", response_class=PlainTextResponse)
async def index():
    return (
        "증시 뉴스 다이제스트 에이전트 — 피어리뷰용 실행기\n\n"
        "GET  /healthz          살아있는지 확인\n"
        "GET  /run?token=...    실행 버튼이 있는 화면으로 이동(브라우저로 링크만 눌러도 됨)\n"
        "                       ?hours=6 으로 수집 시간 창을 줄일 수 있다(기본 24)\n"
        "                       ?publish=1 을 붙이면 디스코드로 실제 발행한다(기본은 dry-run)\n"
        "GET  /bot?token=...    훈련생 문의 응대 봇에게 질문해 보기\n"
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


async def _run_job(job_id: str, hours: int, publish: int) -> None:
    JOBS[job_id]["status"] = "running"
    try:
        os.environ["DRY_RUN"] = "0" if publish else "1"
        graph = build().compile()
        initial = {"hours": hours, "collected": [], "picked": [], "drafted": [], "verified": [], "log": []}
        result = await graph.ainvoke(initial)
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["result"] = {
            "log": result["log"],
            "picked_count": len(result["picked"]),
            "verified_count": len(result["verified"]),
            "articles": [
                {
                    "headline": a.get("headline", ""),
                    "summary": a.get("summary", ""),
                    "insight": a.get("insight", ""),
                    "source": a.get("source", ""),
                    "url": a.get("url", ""),
                }
                for a in result["verified"]
            ],
            "dry_run": not bool(publish),
        }
    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(exc)


@app.get("/run", response_class=HTMLResponse)
async def run_landing(
    token: str = Query(...),
    hours: int = Query(24, ge=1, le=168),
    publish: int = Query(0),
):
    _check_token(token)
    return (
        f"{PAGE_HEAD}<body style='{BODY_STYLE}'>"
        "<h2>증시 뉴스 다이제스트 에이전트</h2>"
        f"<p>최근 {hours}시간 뉴스를 수집해서 5건을 선별하고, 요약·검수까지 실제로 돌려봅니다"
        f"(3~6분 정도 걸려요). {'실제로 디스코드에 발행까지 합니다.' if publish else 'dry-run이라 실제 발행은 하지 않아요.'}</p>"
        "<form method='post' action='/run'>"
        f"<input type='hidden' name='token' value='{token}'>"
        f"<input type='hidden' name='hours' value='{hours}'>"
        f"<input type='hidden' name='publish' value='{publish}'>"
        "<button type='submit' style='font-size:1.2em;padding:14px 28px;background:#2563eb;"
        "color:#fff;border:none;border-radius:8px;cursor:pointer'>실행하기</button>"
        "</form>"
        "</body></html>"
    )


@app.post("/run")
async def run_pipeline(
    token: str = Form(...),
    hours: int = Form(24),
    publish: int = Form(0),
):
    _check_token(token)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "started_at": datetime.now(timezone.utc).isoformat()}
    asyncio.create_task(_run_job(job_id, hours, publish))

    return RedirectResponse(url=f"/status/{job_id}?token={token}", status_code=303)


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status(job_id: str, token: str = Query(...)):
    _check_token(token)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "존재하지 않는 작업입니다(서버가 재시작됐을 수도 있어요).")

    if job["status"] in ("queued", "running"):
        return (
            f"{PAGE_HEAD}<meta http-equiv='refresh' content='5'>"
            f"<body style='{BODY_STYLE}'>"
            "<h3>실행 중입니다... (기사를 하나씩 순서대로 처리해서 3~6분 정도 걸려요)</h3>"
            "<p>이 페이지는 5초마다 자동으로 새로고침돼요. 그냥 기다리시면 됩니다.</p>"
            f"<p style='color:#888'>상태: {job['status']}</p>"
            "</body></html>"
        )

    if job["status"] == "error":
        return (
            f"{PAGE_HEAD}<body style='{BODY_STYLE}'>"
            "<h3>실행 중 오류가 발생했어요</h3>"
            f"<pre style='white-space:pre-wrap'>{job['error']}</pre>"
            "</body></html>"
        )

    r = job["result"]
    if r["articles"]:
        cards = "".join(
            "<div style='border:1px solid #ddd;border-radius:8px;padding:16px;margin:12px 0'>"
            f"<h4 style='margin:0 0 8px'>{a['headline']}</h4>"
            f"<p style='margin:0 0 8px'>{a['summary']}</p>"
            + (f"<p style='color:#2563eb;margin:0 0 8px'>💡 {a['insight']}</p>" if a["insight"] else "")
            + f"<p style='color:#888;font-size:0.85em;margin:0'>출처: {a['source']}"
            + (f" · <a href='{a['url']}' target='_blank'>원문</a>" if a["url"] else "")
            + "</p></div>"
            for a in r["articles"]
        )
    else:
        cards = "<p>검수를 통과한 기사가 없었어요.</p>"
    log = "<br>".join(r["log"])
    return (
        f"{PAGE_HEAD}<body style='{BODY_STYLE}'>"
        "<h2>실행 완료</h2>"
        f"<p>선별 {r['picked_count']}건 → 검수 통과 {r['verified_count']}건</p>"
        f"{cards}"
        f"<p>{'실제 디스코드에 발행함' if not r['dry_run'] else 'dry-run (실제 발행 안 함)'}</p>"
        "<details style='margin-top:16px'><summary style='cursor:pointer;color:#666'>실행 로그 보기</summary>"
        f"<p style='color:#666;font-size:0.9em'>{log}</p></details>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# 훈련생 문의 응대 봇 (my-routing-agent)
# ---------------------------------------------------------------------------
# 뉴스레터와 같은 구조를 쓴다 — 버튼을 누르면 백그라운드로 돌리고 상태 페이지가
# 자동 새로고침한다. 한 건에 10~40초쯤 걸려서 그냥 기다리면 Render가 연결을 끊는다.

# 평가셋(data/goldenset.json 의 split=="eval") 18건 그대로 — REPORT.md 의
# 1.000 / 100% / 100% 이 바로 이 문항들로 잰 값이라, 화면에서 직접 확인할 수 있게 둔다.
EXAMPLES = [
    ("출결", [
        "몇 시까지 들어와야 지각이 아닌가요?",
        "오후에 잠깐 자리를 비워야 하는데 얼마나 비우면 외출 처리해야 하나요?",
        "퇴실 체크를 깜빡했는데 그날 출석은 인정되나요?",
    ]),
    ("공가·휴가", [
        "아이가 아파서 병원 데려갔는데 공가 되나요? 며칠까지 인정돼요?",
        "병원 영수증 내면 공가 처리되나요?",
        "어제 일이 있어서 못 나갔는데 공가 신청은 언제까지 해야 하나요?",
    ]),
    ("장려금·고용형태", [
        "훈련장려금은 하루에 얼마씩 계산되나요?",
        "이번 단위기간에 22일 출석했으면 장려금이 얼마예요?",
        "훈련 중에 주말 알바 하려는데 괜찮을까요?",
    ]),
    ("제적·참여규칙", [
        "결석이 몇 번 쌓이면 제적되나요?",
        "카페에서 QR 찍어도 되나요?",
        "경고 한 번 받으면 바로 제적인가요?",
    ]),
    ("과정 운영", [
        "수료랑 졸업 기준이 어떻게 다른가요?",
        "도메인 개발하기는 뭘 관리하는 건가요?",
        "지금이 몇 번째 단위기간이에요? 오늘 기준으로요.",
    ]),
    ("응대 범위 밖 (넘겨야 정답)", [
        "제가 지금까지 몇 번 결석했나요?",
        "수료하면 취업 알선도 해주나요?",
        "LangGraph에서 조건부 엣지는 어떻게 쓰나요?",
    ]),
]

CAT_KO = {
    "ATTENDANCE": "출결", "LEAVE": "공가·휴가", "ALLOWANCE": "장려금·고용형태",
    "CONDUCT": "제적·참여규칙", "PROGRAM": "과정 운영", "OTHER": "응대 범위 밖",
}


def _openai_ready() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# 데모 주소를 저장소(공개)에 적어 두면 토큰도 같이 공개된다. 토큰은 "링크에 붙는 열쇠"일 뿐
# 로그인이 아니라서, 하루 호출 상한을 따로 둔다 — 누가 반복해서 눌러도 요금이 정해진 만큼만
# 나가게 하려는 것이다. BOT_DAILY_LIMIT 환경변수로 조절한다(0이면 끔).
BOT_DAILY_LIMIT = int(os.environ.get("BOT_DAILY_LIMIT", "30"))
_bot_usage = {"date": "", "count": 0}


def _take_bot_quota() -> bool:
    """오늘 몫이 남아 있으면 하나 쓰고 True. 날짜가 바뀌면 자동으로 초기화된다."""
    if BOT_DAILY_LIMIT <= 0:
        return True
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _bot_usage["date"] != today:
        _bot_usage.update(date=today, count=0)
    if _bot_usage["count"] >= BOT_DAILY_LIMIT:
        return False
    _bot_usage["count"] += 1
    return True


def _ask_sync(question: str) -> dict:
    """무거운 import는 여기서 한다 — 키가 없어도 서버는 뜨게 하려고."""
    import agent  # trainbot/agent.py

    return agent.ask(question)


async def _run_bot_job(job_id: str, question: str) -> None:
    JOBS[job_id]["status"] = "running"
    try:
        JOBS[job_id]["result"] = await asyncio.to_thread(_ask_sync, question)
        JOBS[job_id]["status"] = "done"
    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = f"{type(exc).__name__}: {exc}"


@app.get("/bot", response_class=HTMLResponse)
async def bot_landing(token: str = Query(...), q: str = Query("")):
    _check_token(token)
    if not _openai_ready():
        return (
            f"{PAGE_HEAD}<body style='{BODY_STYLE}'>"
            "<h2>훈련생 문의 응대 봇</h2>"
            "<p>이 봇은 OpenAI 모델(gpt-4o · gpt-6-astra)로 돌아갑니다. "
            "서버에 <code>OPENAI_API_KEY</code>가 아직 설정되지 않아 실행할 수 없어요.</p>"
            "<p style='color:#666'>Render 대시보드 → 이 서비스 → Environment → "
            "<code>OPENAI_API_KEY</code> 추가 후 저장하면 바로 동작합니다.</p>"
            "</body></html>"
        )
    chips = "".join(
        f"<div style='margin:10px 0 4px;color:#666;font-size:0.8em'>{escape(group)}</div>"
        + "".join(
            f"<a href='/bot?token={escape(token)}&q={quote(ex)}' "
            "style='display:inline-block;margin:3px 6px 3px 0;padding:6px 10px;border:1px solid #ccc;"
            f"border-radius:14px;font-size:0.85em;color:#333;text-decoration:none'>{escape(ex)}</a>"
            for ex in items
        )
        for group, items in EXAMPLES
    )
    return (
        f"{PAGE_HEAD}<body style='{BODY_STYLE}'>"
        "<h2>훈련생 문의 응대 봇</h2>"
        "<p>‘AI 에이전트 서비스 개발자 과정’ 공식 공지를 근거 문서로 삼아, 문의를 6개 카테고리로 "
        "나누고 필요한 장(章)만 골라 답합니다. 근거에 없는 내용은 답하지 않고 운영 매니저에게 "
        "넘깁니다. 한 건에 <b>10~40초</b>쯤 걸려요.</p>"
        "<p style='margin-bottom:0;color:#666;font-size:0.9em'>평가셋 18문항 — 눌러서 채우기"
        "<br><span style='font-size:0.85em'>REPORT.md의 분류 1.000 · 도구 호출 100% · 답변 100%가 "
        "바로 이 18문항으로 잰 값입니다.</span></p>"
        f"{chips}"
        "<form method='post' action='/bot' style='margin-top:16px'>"
        f"<input type='hidden' name='token' value='{escape(token)}'>"
        "<textarea name='question' rows='3' required placeholder='궁금한 것을 한 줄로 적어 주세요' "
        "style='width:100%;padding:10px;font-size:1em;font-family:inherit;border:1px solid #ccc;"
        f"border-radius:8px;box-sizing:border-box'>{escape(q)}</textarea>"
        "<button type='submit' style='margin-top:10px;font-size:1.1em;padding:12px 26px;"
        "background:#2563eb;color:#fff;border:none;border-radius:8px;cursor:pointer'>물어보기</button>"
        "</form>"
        "<p style='margin-top:24px;color:#888;font-size:0.85em'>"
        "소스·평가 결과: <a href='https://github.com/smartman98/my-routing-agent' target='_blank'>"
        "github.com/smartman98/my-routing-agent</a>"
        + (f"<br>체험용이라 하루 {BOT_DAILY_LIMIT}건까지만 돌아갑니다"
           f"(오늘 {_bot_usage['count']}건 사용)." if BOT_DAILY_LIMIT > 0 else "")
        + "</p>"
        "</body></html>"
    )


@app.post("/bot")
async def bot_ask(token: str = Form(...), question: str = Form(...)):
    _check_token(token)
    if not _take_bot_quota():
        raise HTTPException(
            429, f"오늘 체험 가능한 질문 수({BOT_DAILY_LIMIT}건)를 다 썼어요. "
                 "내일 다시 시도해 주세요.")
    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "queued", "question": question,
                    "started_at": datetime.now(timezone.utc).isoformat()}
    asyncio.create_task(_run_bot_job(job_id, question))
    return RedirectResponse(url=f"/bot/status/{job_id}?token={token}", status_code=303)


def _fmt_answer(text: str) -> str:
    """모델이 **굵게**나 [링크](url)를 섞어 쓴다 — 이스케이프한 뒤 그것만 되살린다."""
    import re as _re

    html = escape(text)
    html = _re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                   r"<a href='\2' target='_blank'>\1</a>", html)
    html = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
    return html.replace("\n", "<br>")


def _panel(title: str, body: str) -> str:
    return (
        "<div style='border:1px solid #e5e5e5;border-radius:8px;padding:12px 14px;margin:10px 0'>"
        f"<div style='color:#666;font-size:0.8em;margin-bottom:6px'>{title}</div>{body}</div>"
    )


@app.get("/bot/status/{job_id}", response_class=HTMLResponse)
async def bot_status(job_id: str, token: str = Query(...)):
    _check_token(token)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "존재하지 않는 작업입니다(서버가 재시작됐을 수도 있어요).")

    back = f"<p style='margin-top:20px'><a href='/bot?token={escape(token)}'>← 다른 질문 하기</a></p>"
    asked = _panel("문의", f"<b>{escape(job['question'])}</b>")

    if job["status"] in ("queued", "running"):
        return (
            f"{PAGE_HEAD}<meta http-equiv='refresh' content='3'>"
            f"<body style='{BODY_STYLE}'>{asked}"
            "<h3>답변을 만들고 있어요...</h3>"
            "<p>분류 → 근거 조립 → 도구 조회 → 답변 → 검증 순서로 돕니다. 3초마다 자동 새로고침돼요.</p>"
            f"</body></html>"
        )

    if job["status"] == "error":
        return (
            f"{PAGE_HEAD}<body style='{BODY_STYLE}'>{asked}"
            "<h3>오류가 났어요</h3>"
            f"<pre style='white-space:pre-wrap;color:#b91c1c'>{escape(job['error'])}</pre>"
            f"{back}</body></html>"
        )

    r = job["result"]
    cat = r.get("category", "")
    conf = r.get("confidence", 0.0)
    ver = r.get("verification") or {}
    tools = r.get("tools_called") or []
    secs = r.get("sections_used") or []

    if ver.get("ok"):
        ver_html = "<span style='color:#15803d'>✅ 통과 — 근거 없는 숫자·단정 표현 없음</span>"
    elif ver:
        detail = "; ".join(escape(v["detail"]) for v in ver.get("violations", []))
        ver_html = f"<span style='color:#b45309'>⚠ {detail}</span>"
    else:
        ver_html = "<span style='color:#888'>검증 단계를 거치지 않음(범위 밖·이관)</span>"

    return (
        f"{PAGE_HEAD}<body style='{BODY_STYLE}'>{asked}"
        + _panel("답변", f"<div style='font-size:1.05em'>{_fmt_answer(r.get('answer', ''))}</div>")
        + _panel("① 분류", f"<b>{CAT_KO.get(cat, cat)}</b> ({escape(cat)}) · 확신도 {conf:.2f}"
                          f"<div style='color:#666;font-size:0.9em;margin-top:4px'>"
                          f"{escape(r.get('reason', ''))}</div>")
        + _panel("② 사용한 근거 문서", ", ".join(f"{s}장" for s in secs) if secs else "없음")
        + _panel("③ 호출한 조회 도구", ", ".join(f"<code>{escape(t)}</code>" for t in tools)
                 if tools else "없음 (근거 문서만으로 답할 수 있는 문의)")
        + _panel("④ 검증", ver_html)
        + back
        + "</body></html>"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8766)))
