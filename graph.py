"""나만의 뉴스레터 에이전트 — F&G 기반 투자자를 위한 증시 뉴스 다이제스트.

다섯 단계(수집 → 선별 → 요약/인사이트 → 검수 → 발행)를 LangGraph 노드로 구성한다.
AI 모델은 Anthropic Claude(claude_agent_sdk, FABOT이 이미 쓰고 있는 것과 같은 방식)를
쓴다 — 별도 OpenAI 키가 없어도 이 컴퓨터에 이미 로그인된 claude CLI로 바로 돈다.
"""

from __future__ import annotations

import json
import operator
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, TypedDict

import anyio
import feedparser
import requests
import trafilatura
import yaml
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

BASE_DIR = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0 (my-newsletter-agent)"}

# ---------- 설정: 소스 ----------
# 채택 기준·탈락 사유는 REPORT.md의 "소스 채택표" 참고 — 6곳을 실제로 재보고
# (_measure_sources.py) 4곳을 채택, 1곳(CNBC Markets, 24시간 내 신규글 1건뿐)을
# G2(생존) 기준 미달로 제외했다.
SOURCES = [
    ("한국경제", "https://www.hankyung.com/feed/finance"),
    ("연합뉴스 경제", "https://www.yna.co.kr/rss/economy.xml"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

# ---------- 설정: 파이프라인 상수 ----------
# 하루 수집량이 대략 150~200건(연합뉴스 혼자 120건대)이라, 한 화면에 다 놓고 비교할
# 수 없다 — 30건씩 묶어 예선(각 묶음에서 5건)을 치르고, 예선 통과분(대략 25~35건)을
# 모아 본선(상대평가)에서 최종 N건을 고른다(수업 섹션 6~7과 같은 2단 상대평가 구조).
BATCH_SIZE = 30
PRELIM_PER_BATCH = 5
FINAL_PICKS = 5
MAX_PER_SOURCE = 2  # 매체 편중 방지(어제 뉴스레터 실습에서 실제로 겪은 문제의 해결책)
BODY_MIN_LEN = 600  # G1 본문 관문 기준선(수업과 동일)

AUDIENCE = yaml.safe_load((BASE_DIR / "audience.yaml").read_text(encoding="utf-8"))


def build_criteria_text() -> str:
    aud = AUDIENCE["독자"]
    criteria = "\n".join(f"- {c}" for c in AUDIENCE["중요도_기준"])
    drop = "\n".join(f"- {c}" for c in AUDIENCE["버릴_것"])
    topics = "\n".join(f"- {t['이름']}: {t['데스크지침']}" for t in AUDIENCE["토픽"])
    return (
        f"독자: {aud['누구']}\n"
        f"독자가 이미 아는 것: {aud['이미_아는_것']}\n\n"
        f"중요도 기준(위에 있을수록 우선):\n{criteria}\n\n"
        f"버릴 것:\n{drop}\n\n"
        f"토픽별 데스크 지침:\n{topics}\n"
    )


CRITERIA_TEXT = build_criteria_text()


# ---------- Claude 호출 ----------
def _cli_path() -> str | None:
    env = os.environ.get("CLAUDE_CLI_PATH")
    if env:
        return env
    default = Path.home() / ".local" / "bin" / "claude.exe"
    return str(default) if default.exists() else None


async def ask_claude(system_prompt: str, user_prompt: str, max_turns: int = 1) -> str:
    options = ClaudeAgentOptions(
        cli_path=_cli_path(),
        system_prompt=system_prompt,
        allowed_tools=[],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
    )
    text_out = ""
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_out = block.text
    return text_out


def extract_json(text: str):
    """Claude 응답에서 JSON 블록만 뽑아낸다 — 앞뒤에 설명이 붙어도 견딘다."""
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSON을 찾을 수 없음: {text[:200]}")
    return json.loads(match.group(0))


# ---------- State ----------
class NewsletterState(TypedDict):
    hours: int
    collected: list
    picked: list
    drafted: Annotated[list, operator.add]
    verified: list
    log: Annotated[list, operator.add]


# ---------- ① 수집 ----------
def collect(state: dict) -> dict:
    hours = state["hours"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_urls = set()
    collected = []
    dead = []

    for name, url in SOURCES:
        try:
            resp = requests.get(url, headers=UA, timeout=15)
            feed = feedparser.parse(resp.content)
            count_before = len(collected)
            for e in feed.entries:
                pub = e.get("published_parsed") or e.get("updated_parsed")
                if not pub:
                    continue
                dt = datetime(*pub[:6], tzinfo=timezone.utc)
                if dt < cutoff:
                    continue
                key = e.link.split("?")[0]
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                collected.append(
                    {
                        "title": e.title,
                        "url": e.link,
                        "source": name,
                        "published": dt.isoformat(),
                        "summary": (e.get("summary", "") or "")[:300],
                    }
                )
            got = len(collected) - count_before
            if got == 0:
                dead.append(name)
        except Exception as exc:  # noqa: BLE001
            dead.append(f"{name}({exc})")
        time.sleep(0.3)

    log = [f"① 수집   {hours}시간 창 · {len(collected)}건 (소스 {len(SOURCES)}곳, 무응답/0건: {dead or '없음'})"]
    return {"collected": collected, "log": log}


# ---------- ② 선별 (예선 → 본선) ----------
async def _prelim_batch(batch: list[dict]) -> list[dict]:
    listing = "\n".join(f"{i}. [{a['source']}] {a['title']}" for i, a in enumerate(batch))
    system = (
        "당신은 증시 뉴스 편집자입니다. 아래 기준으로 이 묶음에서 가장 중요한 "
        f"{PRELIM_PER_BATCH}건의 번호만 고르세요.\n\n{CRITERIA_TEXT}\n"
        '반드시 JSON 배열로만 답하세요. 형식: [0, 3, 7, ...] (번호만, 설명 없이)'
    )
    text = await ask_claude(system, listing)
    try:
        idxs = extract_json(text)
        return [batch[i] for i in idxs if isinstance(i, int) and 0 <= i < len(batch)]
    except Exception:  # noqa: BLE001
        return batch[:PRELIM_PER_BATCH]  # 파싱 실패 시 안전하게 앞에서부터만


async def _final_pick(shortlisted: list[dict]) -> list[dict]:
    listing = "\n".join(f"{i}. [{a['source']}] {a['title']}" for i, a in enumerate(shortlisted))
    system = (
        "당신은 증시 뉴스 편집자입니다. 아래 후보 전체를 놓고 비교해서 오늘 뉴스레터에 "
        f"실을 상위 {FINAL_PICKS}건을 고르세요.\n\n{CRITERIA_TEXT}\n"
        "같은 사건(같은 발표, 같은 이슈)을 다룬 기사가 여러 개면 그 중 하나만 고르세요. "
        '각 선택에 event(사건을 짧게 대표하는 라벨, 같은 사건이면 같은 라벨)와 '
        "reason(왜 골랐는지 한 문장)을 붙여, 다음 JSON 배열 형식으로만 답하세요:\n"
        '[{"idx": 0, "event": "연준 금리동결", "reason": "..."}]'
    )
    text = await ask_claude(system, listing)
    picks = extract_json(text)
    out = []
    for p in picks:
        i = p.get("idx")
        if isinstance(i, int) and 0 <= i < len(shortlisted):
            item = dict(shortlisted[i])
            item["event"] = p.get("event", "")
            item["reason"] = p.get("reason", "")
            out.append(item)
    return out


def _dedup_and_cap(picks: list[dict]) -> tuple[list[dict], list[str]]:
    """같은 사건 중복 제거 + 매체별 상한 — 어제 뉴스레터 실습에서 실제로 확인한
    문제(선별 개선점 md 참고)를 여기서는 처음부터 코드로 막는다."""
    seen_events, source_count, kept, dropped_reasons = set(), {}, [], []
    for p in picks:
        ev = p.get("event") or p["title"]
        if ev in seen_events:
            dropped_reasons.append(f"중복사건 제외: {p['title'][:30]}")
            continue
        if source_count.get(p["source"], 0) >= MAX_PER_SOURCE:
            dropped_reasons.append(f"매체상한 제외: {p['title'][:30]}")
            continue
        seen_events.add(ev)
        source_count[p["source"]] = source_count.get(p["source"], 0) + 1
        kept.append(p)
    return kept, dropped_reasons


async def select(state: dict) -> dict:
    collected = state["collected"]
    batches = [collected[i : i + BATCH_SIZE] for i in range(0, len(collected), BATCH_SIZE)]
    shortlisted: list[dict] = []
    for b in batches:
        shortlisted.extend(await _prelim_batch(b))

    picks = await _final_pick(shortlisted) if shortlisted else []
    final, dropped = _dedup_and_cap(picks)
    final = final[:FINAL_PICKS]

    log = [
        f"② 선별   {len(collected)}건 → 묶음 {len(batches)}개 → 예선 {len(shortlisted)}건 → "
        f"본선 {len(picks)}건 → 중복/상한 제거 {len(dropped)}건 → 최종 {len(final)}건"
    ]
    if dropped:
        log.append("   " + " / ".join(dropped))
    return {"picked": final, "log": log}


# ---------- ③ 요약 + 인사이트 ----------
async def report_one(payload: dict) -> dict:
    item = payload["item"]
    downloaded = trafilatura.fetch_url(item["url"])
    body = trafilatura.extract(downloaded) if downloaded else None
    if not body or len(body) < BODY_MIN_LEN:
        return {"drafted": [], "log": [f"   취재 실패(본문 부족): {item['title'][:30]}"]}

    system = (
        "당신은 F&G 지수 기반 역발상 투자자를 위한 증시 뉴스 기자입니다. 아래 기사 "
        "본문을 읽고 headline(20자 내외 한국어), summary(세 문장, ~습니다체, 건조하게), "
        "insight(이 투자자에게 왜 중요한지 한 문장 — summary에 있는 내용만 근거로, "
        "새 사실을 추가하지 말 것)를 만드세요. "
        '반드시 JSON으로만 답하세요: {"headline": "...", "summary": "...", "insight": "..."}'
    )
    text = await ask_claude(system, body[:6000])
    try:
        parsed = extract_json(text)
    except Exception as exc:  # noqa: BLE001
        return {"drafted": [], "log": [f"   취재 실패(파싱 오류 {exc}): {item['title'][:30]}"]}

    draft = {**item, "body": body, **parsed}
    return {"drafted": [draft], "log": [f"   취재 완료: {parsed.get('headline', '')}"]}


def fanout_to_report(state: dict):
    if not state["picked"]:
        # 고른 기사가 0건이면 report_one_node로 팬아웃할 게 없다 — 그래도 검수·발행
        # 단계는 돌아야 "오늘은 조용합니다" 카드가 나간다.
        return "verify"
    return [Send("report_one_node", {"item": item}) for item in state["picked"]]


# ---------- ④ 검수 ----------
def _numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,\.]*%?", text or ""))


def _hallucination_check(draft: dict) -> tuple[bool, str]:
    """summary/insight에 나온 숫자가 원문(headline+summary)에도 있는지 대조한다
    (수업 섹션 9와 같은 방식 — 문자열 대조, 번역/단위환산은 못 잡는 한계는 동일)."""
    body_numbers = _numbers_in(draft.get("body", ""))
    summary_numbers = _numbers_in(draft.get("summary", ""))
    missing = summary_numbers - body_numbers
    if missing:
        return False, f"본문에 없는 숫자: {missing}"
    insight_numbers = _numbers_in(draft.get("insight", ""))
    summary_and_body = summary_numbers | body_numbers
    missing_insight = insight_numbers - summary_and_body
    if missing_insight:
        return False, f"insight에 근거 없는 숫자: {missing_insight}"
    if not draft.get("headline") or not draft.get("summary"):
        return False, "headline/summary 비어있음"
    return True, ""


async def verify(state: dict) -> dict:
    verified, log = [], []
    for draft in state["drafted"]:
        ok, reason = _hallucination_check(draft)
        if ok:
            verified.append(draft)
            log.append(f"④ 검수 합격: {draft['headline']}")
            continue

        log.append(f"④ 검수 불합격({reason}), 재생성 시도: {draft['title'][:30]}")
        retried = await report_one({"item": {k: draft[k] for k in ("title", "url", "source", "published", "summary")}})
        new_drafts = retried["drafted"]
        if new_drafts:
            ok2, reason2 = _hallucination_check(new_drafts[0])
            if ok2:
                verified.append(new_drafts[0])
                log.append(f"   재생성 후 합격: {new_drafts[0]['headline']}")
                continue
            log.append(f"   재생성 후에도 불합격({reason2}) — 이 건은 스킵")
        else:
            log.append("   재생성 실패 — 이 건은 스킵")

    return {"verified": verified, "log": log}


# ---------- ⑤ 발행 ----------
COLORS = {"거시경제": 0x0B6E77, "지수·시황": 0x4C7C9C, "원자재·환율": 0x8F5606, "정책·규제": 0x2E7D5B}
DEFAULT_COLOR = 0x5F7476
EMBED_MAX, TOTAL_MAX = 10, 5800


def build_embeds(run_id: str, verified: list[dict]) -> list[dict]:
    if not verified:
        return [{"title": f"🗞️ {run_id}", "color": DEFAULT_COLOR, "description": "오늘은 조용합니다."}]
    embeds = [
        {
            "title": f"🗞️ {run_id} · 증시 뉴스 다이제스트",
            "description": f"오늘은 {len(verified)}건을 골랐습니다.",
            "color": DEFAULT_COLOR,
        }
    ]
    for i, a in enumerate(verified, 1):
        desc = a["summary"]
        if a.get("insight"):
            desc += f"\n\n💡 **{a['insight']}**"
        embeds.append(
            {
                "title": f"{i}. {a['headline']}"[:256],
                "description": desc[:4096],
                "url": a["url"],
                "color": DEFAULT_COLOR,
                "footer": {"text": a["source"]},
            }
        )
    total = lambda es: sum(len(e.get("title", "")) + len(e.get("description", "")) for e in es)
    while len(embeds) > EMBED_MAX or total(embeds) > TOTAL_MAX:
        embeds.pop()
    return embeds


def publish(state: dict) -> dict:
    run_id = datetime.now().strftime("%Y-%m-%d")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    dry_run = os.environ.get("DRY_RUN", "1") != "0"

    embeds = build_embeds(run_id, state["verified"])
    payload = {"username": "증시 다이제스트", "embeds": embeds}

    sent = False
    if dry_run or not webhook:
        log = [f"⑤ 발행   {len(state['verified'])}건 · dry-run (실제 발송 안 함)"]
    else:
        resp = requests.post(webhook, json=payload, timeout=20)
        sent = resp.status_code in (200, 204)
        log = [f"⑤ 발행   {len(state['verified'])}건 · {'성공' if sent else f'실패({resp.status_code})'}"]

    metrics = {
        "run_id": run_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "collected": len(state.get("collected", [])),
        "picked": len(state.get("picked", [])),
        "drafted": len(state.get("drafted", [])),
        "verified": len(state.get("verified", [])),
        "published": sent,
        "dry_run": dry_run,
    }
    store_dir = BASE_DIR / "store"
    store_dir.mkdir(exist_ok=True)
    with (store_dir / "metrics.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + "\n")

    return {"log": log}


# ---------- 그래프 조립 ----------
def build():
    g = StateGraph(NewsletterState)
    g.add_node("collect", collect)
    g.add_node("select", select)
    g.add_node("report_one_node", report_one)
    g.add_node("verify", verify)
    g.add_node("publish", publish)

    g.add_edge(START, "collect")
    g.add_edge("collect", "select")
    g.add_conditional_edges("select", fanout_to_report, ["report_one_node", "verify"])
    g.add_edge("report_one_node", "verify")
    g.add_edge("verify", "publish")
    g.add_edge("publish", END)
    return g
