"""피어리뷰용 — 리뷰어가 아이디/비밀번호 없이 링크만 눌러서 파이프라인을 실제로
돌려볼 수 있게 하는 최소한의 웹 서버(2026-09-15 사용자 요청).

로그인 화면은 없지만, 아무나 눌러도 실제 Claude 호출 비용이 나가지 않도록
/run에는 URL에 붙이는 짧은 토큰(REVIEW_TOKEN) 하나만 요구한다 — "로그인"이
아니라 그냥 공유 링크에 붙는 열쇠다.
"""

import os

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from graph import build

app = FastAPI(title="증시 뉴스 다이제스트 — 피어리뷰용 실행기")


@app.get("/", response_class=PlainTextResponse)
async def index():
    return (
        "증시 뉴스 다이제스트 에이전트 — 피어리뷰용 실행기\n\n"
        "GET  /healthz          살아있는지 확인\n"
        "GET|POST /run?token=... 실제로 파이프라인을 한 번 돌린다(1~2분 걸림, 브라우저로 링크만 눌러도 됨)\n"
        "                       ?hours=6 으로 수집 시간 창을 줄일 수 있다(기본 24)\n"
        "                       ?publish=1 을 붙이면 디스코드로 실제 발행한다(기본은 dry-run)\n"
    )


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.api_route("/run", methods=["GET", "POST"])
async def run_pipeline(
    token: str = Query(...),
    hours: int = Query(24, ge=1, le=168),
    publish: int = Query(0),
):
    expected = os.environ.get("REVIEW_TOKEN")
    if not expected or token != expected:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")

    # 리뷰어가 여러 번 눌러도 실제 디스코드 채널에 스팸이 안 가도록, 기본은 dry-run이다.
    os.environ["DRY_RUN"] = "0" if publish else "1"

    graph = build().compile()
    initial = {"hours": hours, "collected": [], "picked": [], "drafted": [], "verified": [], "log": []}
    result = await graph.ainvoke(initial)

    return {
        "log": result["log"],
        "picked_count": len(result["picked"]),
        "verified_count": len(result["verified"]),
        "verified_headlines": [a["headline"] for a in result["verified"]],
        "dry_run": not bool(publish),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8766)))
