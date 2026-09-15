"""나만의 뉴스레터 에이전트 실행 스크립트.

사용법:
    python run.py            # dry-run(디스코드로 실제 발송하지 않음)
    DRY_RUN=0 python run.py  # 실제 발행

환경변수:
    DISCORD_WEBHOOK_URL   디스코드 웹훅 주소
    DRY_RUN               "0"이면 실제 발행, 그 외/미설정이면 dry-run(기본값)
    CLAUDE_CLI_PATH       claude CLI 경로(생략 시 ~/.local/bin/claude.exe 자동 탐색)
"""

import sys

import anyio

from graph import build


async def main(hours: int) -> None:
    graph = build().compile()
    initial = {"hours": hours, "collected": [], "picked": [], "drafted": [], "verified": [], "log": []}
    result = await graph.ainvoke(initial)

    print("=" * 60)
    for line in result["log"]:
        print(line)
    print("=" * 60)
    if result["verified"]:
        print(f"\n최종 발행 {len(result['verified'])}건:")
        for a in result["verified"]:
            print(f"- {a['headline']} ({a['source']})")
    else:
        print("\n오늘은 조용합니다(선별된 기사 없음).")


if __name__ == "__main__":
    hours_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    anyio.run(main, hours_arg)
