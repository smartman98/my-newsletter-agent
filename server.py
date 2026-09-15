"""피어리뷰용 — 리뷰어가 아이디/비밀번호 없이 링크만 눌러서 파이프라인을 실제로
돌려볼 수 있게 하는 최소한의 웹 서버(2026-09-15 사용자 요청).

로그인 화면은 없지만, 아무나 눌러도 실제 Claude 호출 비용이 나가지 않도록
/run에는 URL에 붙이는 짧은 토큰(REVIEW_TOKEN) 하나만 요구한다 — "로그인"이
아니라 그냥 공유 링크에 붙는 열쇠다.

/run은 파이프라인을 백그라운드로 시작만 하고 바로 결과 확인 페이지로
넘긴다(원래는 끝날 때까지 붙잡고 있었는데, Render 무료 플랜이 1분 안팎에서
연결을 끊어버려 "헛바퀴"처럼 보이는 502가 발생했음 — 2026-09-15 확인).
"""

import asyncio
import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from graph import build

app = FastAPI(title="증시 뉴스 다이제스트 — 피어리뷰용 실행기")

JOBS: dict[str, dict] = {}


def _check_token(token: str) -> None:
    expected = os.environ.get("REVIEW_TOKEN")
    if not expected or token != expected:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")


@app.get("/", response_class=PlainTextResponse)
async def index():
    return (
        "증시 뉴스 다이제스트 에이전트 — 피어리뷰용 실행기\n\n"
        "GET  /healthz          살아있는지 확인\n"
        "GET|POST /run?token=... 파이프라인 실행을 요청한다(브라우저로 링크만 눌러도 됨).\n"
        "                       바로 결과 확인 페이지로 넘어가고, 1~2분 뒤 자동으로 결과가 뜬다.\n"
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
            "verified_headlines": [a["headline"] for a in result["verified"]],
            "dry_run": not bool(publish),
        }
    except Exception as exc:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(exc)


@app.api_route("/run", methods=["GET", "POST"])
async def run_pipeline(
    token: str = Query(...),
    hours: int = Query(24, ge=1, le=168),
    publish: int = Query(0),
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
            "<html><head><meta charset='utf-8'><meta http-equiv='refresh' content='5'></head>"
            "<body style='font-family:sans-serif'>"
            "<h3>실행 중입니다... (보통 1~2분 걸려요)</h3>"
            "<p>이 페이지는 5초마다 자동으로 새로고침돼요. 그냥 기다리시면 됩니다.</p>"
            f"<p style='color:#888'>상태: {job['status']}</p>"
            "</body></html>"
        )

    if job["status"] == "error":
        return (
            "<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif'>"
            "<h3>실행 중 오류가 발생했어요</h3>"
            f"<pre>{job['error']}</pre>"
            "</body></html>"
        )

    r = job["result"]
    headlines = "".join(f"<li>{h}</li>" for h in r["verified_headlines"]) or "<li>(없음)</li>"
    log = "<br>".join(r["log"])
    return (
        "<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif'>"
        "<h3>실행 완료</h3>"
        f"<p>선별 {r['picked_count']}건 → 검수 통과 {r['verified_count']}건</p>"
        f"<ul>{headlines}</ul>"
        f"<p>{'실제 디스코드에 발행함' if not r['dry_run'] else 'dry-run (실제 발행 안 함)'}</p>"
        f"<hr><p style='color:#666;font-size:0.9em'>{log}</p>"
        "</body></html>"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8766)))
