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
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from graph import build

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8766)))
